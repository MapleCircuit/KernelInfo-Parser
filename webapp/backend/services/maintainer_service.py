"""webapp/backend/services/maintainer_service.py - Subsystems Catalog, Maintainers Roster & CREDITS."""
from __future__ import annotations
from webapp.backend.config import RepoScopedCache
from typing import Any
from fastapi import HTTPException
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import (
    tree_source,
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
from parser.maintainer_ast import maintainer_store
from parser.git_ast.identity import build_identity_index, clean_name, identity_key, merge_emails

# Bounded: keyed by the requested version, and any revision a caller names (a commit SHA) adds a whole
# MAINTAINERS model
_MAINTAINER_CACHE: dict[str, tuple[list[MaintainerSection], MaintainerMatcher]] = RepoScopedCache(maxsize=16)
# Bounded: the key carries the requested file path, so each distinct path a caller sends adds an entry
_FILE_SUBSYSTEMS_CACHE: dict[tuple[str, str], list[dict[str, Any]]] = RepoScopedCache(maxsize=4096)
# Sections of one MAINTAINERS blob / entries of one CREDITS blob (content addressed, keyed by blob_id)
_BLOB_SECTIONS_CACHE: dict[int, list[int]] = RepoScopedCache(maxsize=64)
# Whether m_maintainer_file has the per-section layout (core/DBLayout.py table 19); an older database keyed
# by MAINTAINERS blob holds incomplete rows, so it is not read
_MAINT_FILE_LAYOUT: dict[str, bool] = RepoScopedCache(maxsize=2)
# Whether m_maintainer_section has extended columns (patchwork, bug_url, chat_channel, profile_doc)
_EXTENDED_SEC_COLS: dict[str, bool] = RepoScopedCache(maxsize=2)
# Files of a section detail: page size
SECTION_FILES_LIMIT = 500
_BLOB_CREDITS_CACHE: dict[int, list[int]] = RepoScopedCache(maxsize=64)
# (emails, names) of the version's CREDITS entries, for the in_credits flags of resolve_subsystems_for_file
_CREDITS_KEYS_CACHE: dict[str, tuple[set[str], set[str]]] = RepoScopedCache(maxsize=16)
# Persona identity index (see MaintainerService._identity_index), keyed by registry signature
_IDENTITY_CACHE: dict[str, Any] = RepoScopedCache(maxsize=4)
# Files of one patch that match_maintainers() resolves; the rest are reported as skipped
MAX_PATCH_FILES = 200


# Identity rule shared with the parser's release statistics: parser/git_ast/identity.py
_clean_name = clean_name
_identity_key = identity_key


# Shared with the parser, which stores the index in m_person_identity (parser/git_ast/identity.py)
_merge_emails = merge_emails


def _domain_query(query: str | None) -> str | None:
    """Domain of an `@domain` search (`"@Intel.com"` -> `"intel.com"`), or None when the query is not one.

    A bare `@`, or a query with a second `@` or whitespace, stays an ordinary substring search."""
    q = (query or "").strip()[:100]
    if not q.startswith("@"):
        return None
    domain = q[1:].strip(".").casefold()
    if not domain or "@" in domain or any(ch.isspace() for ch in domain):
        return None
    return domain


def _address_in_domain(addr: str, domain: str) -> bool:
    """True when addr's host is domain or one of its subdomains (`x@linux.intel.com` for `intel.com`).

    Surrounding `<>`, `[]`, `()` and quotes are ignored; `intel.com.au` and `lintel.com` do not match."""
    clean = addr.strip().strip("<>[]()\"'").casefold()
    local, sep, host = clean.rpartition("@")
    if not sep or not local:
        return False
    host = host.rstrip(".")
    return host == domain or host.endswith("." + domain)


def _emails_in_domain(field: str | None, domain: str) -> list[str]:
    """Addresses of a (possibly comma-joined) email field that belong to domain."""
    return [a for a in _merge_emails([field]) if _address_in_domain(a, domain)]


def _in_ids(column: str, ids: list[int] | None) -> tuple[str, list[int]]:
    """SQL condition `column IN (ids)` and its parameters; never true for an empty or missing list."""
    if not ids:
        return "1 = 0", []
    return f"{column} IN ({','.join(['%s'] * len(ids))})", list(ids)


_build_identity_index = build_identity_index


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
                FROM m_checkpoint_tree bc
                JOIN m_file_name fn ON bc.fnid = fn.fnid
                WHERE bc.commit_id = %s AND fn.fname IN ('MAINTAINERS', 'CREDITS');
                """.replace("m_checkpoint_tree", tree_source(cursor)),
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
        """sec_ids of the section versions one MAINTAINERS blob holds, one per section name.

        Sections are content addressed (an unchanged section keeps the row of an earlier blob) and one
        AST names every stored version of a section: the version the blob holds is chosen by
        parser/maintainer_ast/maintainer_store.py, which Step 6.7 uses too.
        """
        cached = _BLOB_SECTIONS_CACHE.get(blob_id)
        if cached is not None:
            return cached
        ids = sorted(maintainer_store.blob_section_ids(cursor, blob_id).values())
        _BLOB_SECTIONS_CACHE[blob_id] = ids
        return ids

    @staticmethod
    def _blob_credit_ids(cursor: Any, blob_id: int) -> list[int]:
        """credit_ids of the entry versions one CREDITS blob holds (chosen as sections are)."""
        cached = _BLOB_CREDITS_CACHE.get(blob_id)
        if cached is not None:
            return cached
        ids = maintainer_store.blob_credit_ids(cursor, blob_id)
        _BLOB_CREDITS_CACHE[blob_id] = ids
        return ids

    @staticmethod
    def _section_file_rows_readable(cursor: Any) -> bool:
        """True when m_maintainer_file has the per-section layout (rows complete per section version)."""
        hit = _MAINT_FILE_LAYOUT.get("per_section")
        if hit is not None:
            return hit
        try:
            cursor.execute(
                "SELECT COUNT(*) AS n FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'm_maintainer_file' AND COLUMN_NAME = 'maintainers_blob_id'"
            )
            row = cursor.fetchone() or {}
            n = row.get("n") if isinstance(row, dict) else row[0]
        except Exception:  # noqa: BLE001 - unreadable: use the matcher
            return False
        if not isinstance(n, int):
            return False
        ok = n == 0
        _MAINT_FILE_LAYOUT["per_section"] = ok
        return ok

    @staticmethod
    def _has_extended_section_columns(cursor: Any) -> bool:
        """True when m_maintainer_section has extended columns (patchwork, bug_url, chat_channel, profile_doc)."""
        hit = _EXTENDED_SEC_COLS.get("has_cols")
        if hit is not None:
            return hit
        try:
            cursor.execute(
                "SELECT COUNT(*) AS n FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'm_maintainer_section' AND COLUMN_NAME = 'patchwork'"
            )
            row = cursor.fetchone() or {}
            n = row.get("n") if isinstance(row, dict) else row[0]
            ok = bool(isinstance(n, int) and n > 0)
        except Exception:  # noqa: BLE001 - unreadable: assume standard schema
            ok = False
        _EXTENDED_SEC_COLS["has_cols"] = ok
        return ok

    @staticmethod
    def _section_select_cols(cursor: Any) -> str:
        """Columns for SELECT from m_maintainer_section."""
        if MaintainerService._has_extended_section_columns(cursor):
            return "s.sec_id, s.name, s.status, s.scm_tree, s.web_page, s.mailing_list, s.ast_id, s.patchwork, s.bug_url, s.chat_channel, s.profile_doc"
        return "s.sec_id, s.name, s.status, s.scm_tree, s.web_page, s.mailing_list, s.ast_id"

    @staticmethod
    def _stored_sections_for_paths(cursor: Any, paths: list[str], sec_ids: list[int] | None) -> dict[str, set[int]]:
        """path -> sec_ids of the version (sec_ids) that m_maintainer_file maps it to; paths without rows
        (every path matches THE REST, so none means "not computed yet") are left out."""
        if not paths or not sec_ids or not MaintainerService._section_file_rows_readable(cursor):
            return {}
        fmt = ",".join(["%s"] * len(paths))
        cursor.execute(f"SELECT fnid, fname FROM m_file_name WHERE fname IN ({fmt});", tuple(paths))
        by_fnid = {int(r["fnid"]): safe_decode(r["fname"]) for r in cursor.fetchall()}
        if not by_fnid:
            return {}
        wanted = set(int(s) for s in sec_ids)
        out: dict[str, set[int]] = {}
        fnids = sorted(by_fnid)
        cursor.execute(
            f"SELECT fnid, sec_id FROM m_maintainer_file WHERE fnid IN ({','.join(['%s'] * len(fnids))});",
            tuple(fnids),
        )
        for r in cursor.fetchall():
            sid = int(r["sec_id"])
            if sid in wanted:
                out.setdefault(by_fnid[int(r["fnid"])], set()).add(sid)
        return out

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
        Read from m_person_identity, which the parser refreshes after each version
        (parser/git_ast/identity.py); persons without a stored row (registered since its last refresh,
        whatever their person_id) get their own key (`identity_key()`, without the name-less merge) until
        the next one. Without stored rows (an older
        database) the index is computed from the whole registry. Cached per repository until the
        registry or the stored identities change."""
        cursor.execute("SELECT COUNT(*) AS cnt, MAX(person_id) AS max_id FROM m_maintainer_person;")
        sig_row = cursor.fetchone() or {}
        stored_cnt = stored_max = 0
        try:
            cursor.execute("SELECT COUNT(*) AS cnt, MAX(person_id) AS max_id FROM m_person_identity;")
            st = cursor.fetchone() or {}
            stored_cnt, stored_max = int(st.get("cnt") or 0), int(st.get("max_id") or 0)
        except Exception:  # noqa: BLE001 - a database without the table: compute the index
            stored_cnt = stored_max = 0
        sig = (int(sig_row.get("cnt") or 0), int(sig_row.get("max_id") or 0), stored_cnt, stored_max)
        cached = _IDENTITY_CACHE.get("index")
        if cached is not None and cached[0] == sig:
            return cached[1], cached[2]
        if stored_cnt:
            cursor.execute("SELECT person_id, identity_key FROM m_person_identity;")
            pid_to_key = {int(r["person_id"]): safe_decode(r["identity_key"]) for r in cursor.fetchall()}
            # Every person without a stored row, not only those above its highest person_id: persons are
            # not registered in person_id order, so a refresh can leave gaps below it
            cursor.execute(
                "SELECT p.person_id, p.name, p.email FROM m_maintainer_person p"
                " LEFT JOIN m_person_identity i ON i.person_id = p.person_id"
                " WHERE p.person_id > 0 AND i.person_id IS NULL;"
            )
            for r in cursor.fetchall():
                pid_to_key.setdefault(int(r["person_id"]), _identity_key(safe_decode(r["name"]), safe_decode(r["email"])))
        else:
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

    @staticmethod
    def _domain_person_ids(cursor: Any, domain: str) -> list[int]:
        """person_ids of the personas with an address in domain or one of its subdomains.

        LIKE narrows the scan; the exact host test runs per address, since an email field can hold
        several comma-joined addresses."""
        cursor.execute(
            "SELECT person_id, email FROM m_maintainer_person WHERE person_id > 0 AND LOWER(email) LIKE %s;",
            (f"%{sanitize_like_query(domain)}%",),
        )
        return [int(r["person_id"]) for r in cursor.fetchall() if _emails_in_domain(safe_decode(r["email"]), domain)]

    def get_overview(self, version_name: str, query: str = "") -> dict[str, Any]:
        """List subsystem sections and maintainers with optional query filtering.

        An `@domain` query keeps the sections with a member whose address is in that domain."""
        target_q = query
        domain = _domain_query(target_q)
        clean_q = sanitize_like_query(target_q)
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            sec_ids, _ = self._version_scope(cursor, vid, vname)
            in_secs, params = _in_ids("s.sec_id", sec_ids)

            cols = self._section_select_cols(cursor)
            sql = f"""
                SELECT {cols}
                FROM m_maintainer_section s
                WHERE {in_secs}
            """
            if domain:
                in_pids, pid_params = _in_ids("mem.person_id", self._domain_person_ids(cursor, domain))
                sql += f" AND EXISTS (SELECT 1 FROM m_maintainer_member mem WHERE mem.sec_id = s.sec_id AND {in_pids})"
                params.extend(pid_params)
            elif clean_q:
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
                    "patchwork": safe_decode(s.get("patchwork")),
                    "bug_url": safe_decode(s.get("bug_url")),
                    "chat_channel": safe_decode(s.get("chat_channel")),
                    "profile_doc": safe_decode(s.get("profile_doc")),
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

            # Search matches any alias's name or address, and keeps the whole merged identity.
            # An `@domain` search matches the aliases' addresses in that domain or its subdomains.
            domain = _domain_query(target_q)
            needle = (target_q or "").strip()[:100].casefold()
            matched_by_key: dict[str, list[str]] = {}
            if domain:
                for k, g in groups.items():
                    hits = _merge_emails([a for r in g for a in _emails_in_domain(safe_decode(r["email"]), domain)])
                    if hits:
                        matched_by_key[k] = hits
                groups = {k: g for k, g in groups.items() if k in matched_by_key}
            elif needle:
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
            for key, g in groups.items():
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

                dev = {
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
                }
                if domain:
                    # Representative's own address first when it is one of the matches
                    hits = matched_by_key[key]
                    rep_hits = _emails_in_domain(p_email, domain)
                    dev["matched_emails"] = _merge_emails(rep_hits + hits)
                developers.append(dev)

            if clean_sort == "alpha":
                developers.sort(key=lambda d: ((d["name"] or d["email"] or "").casefold(), -d["subsystems_count"]))
            else:
                developers.sort(key=lambda d: (-d["subsystems_count"], (d["name"] or d["email"] or "").casefold()))

            return {
                "version": vname,
                "sort": clean_sort,
                "developers": developers,
            }

    def _credited_identities(self, cursor: Any, credit_ids: list[int] | None) -> set[str]:
        """Identity keys (see _identity_index) of the people with an entry among credit_ids."""
        if not credit_ids:
            return set()
        in_sql, params = _in_ids("credit_id", credit_ids)
        cursor.execute(f"SELECT DISTINCT person_id FROM m_credits_entry WHERE {in_sql};", tuple(params))
        pids = [int(r["person_id"]) for r in cursor.fetchall()]
        pid_to_key, _ = self._identity_index(cursor)
        return {pid_to_key.get(pid, f"p:{pid}") for pid in pids}

    def _section_files(self, cursor: Any, sec_id: int, vid: int | None, vname: str) -> list[str]:
        """Sorted paths of the version's tree that the section version covers (m_maintainer_file; both
        layouts have the sec_id and fnid columns)."""
        cursor.execute(
            """
            SELECT fn.fname
            FROM m_maintainer_file mf
            JOIN m_file_name fn ON mf.fnid = fn.fnid
            WHERE mf.sec_id = %s;
            """,
            (sec_id,),
        )
        paths = {safe_decode(r["fname"]) for r in cursor.fetchall()}
        if not paths:
            return []
        from webapp.backend.services.tree_resolver import git_tree_resolver
        tree = git_tree_resolver.list_tree(vname)
        if tree is not None:
            return sorted(p for p in paths if p in tree)
        if vid and vid > 0:
            cursor.execute(
                f"""
                SELECT fn.fname
                FROM m_maintainer_file mf
                JOIN {tree_source(cursor)} t ON t.commit_id = %s AND t.fnid = mf.fnid
                JOIN m_file_name fn ON mf.fnid = fn.fnid
                WHERE mf.sec_id = %s;
                """,
                (vid, sec_id),
            )
            return sorted({safe_decode(r["fname"]) for r in cursor.fetchall()})
        return []

    def get_section_detail(self, version_name: str, section_name: str) -> dict[str, Any]:
        """Retrieve subsystem details, pattern rules, and file matches."""
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            sec_ids, credit_ids = self._version_scope(cursor, vid, vname)
            in_secs_sql, sec_params = _in_ids("s.sec_id", sec_ids)

            sec_param = str(section_name).strip()
            cols = self._section_select_cols(cursor)
            if sec_param.isdigit():
                cursor.execute(
                    f"""
                    SELECT {cols}
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
                    SELECT {cols}
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

            # Members; in_credits: an alias of the member has an entry in the version's CREDITS file
            cursor.execute(
                """
                SELECT p.person_id, p.name, p.email, m.role_type
                FROM m_maintainer_member m
                JOIN m_maintainer_person p ON m.person_id = p.person_id
                WHERE m.sec_id = %s
                ORDER BY m.priority ASC;
                """,
                (sec_id,),
            )
            member_rows = cursor.fetchall()
            credited = self._credited_identities(cursor, credit_ids)
            pid_to_key, _ = self._identity_index(cursor) if credited else ({}, {})
            members = [
                {
                    "person_id": m["person_id"],
                    "name": safe_decode(m["name"]),
                    "email": safe_decode(m["email"]),
                    "role_type": m["role_type"],
                    "role": "Maintainer" if m["role_type"] == 1 else "Reviewer",
                    "role_name": "Maintainer" if m["role_type"] == 1 else "Reviewer",
                    "in_credits": pid_to_key.get(int(m["person_id"]), f"p:{m['person_id']}") in credited,
                }
                for m in member_rows
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

            # Files of the section version present in this version's tree (first SECTION_FILES_LIMIT by path)
            all_files = self._section_files(cursor, int(sec_id), vid, vname)
            files = [{"fname": f} for f in all_files[:SECTION_FILES_LIMIT]]

            # Extended section attributes (patchwork, bug_url, chat_channel)
            attrs: dict[str, str] = {}
            if sec_row.get("ast_id"):
                cursor.execute(
                    """
                    SELECT name, val
                    FROM m_ast_attribute
                    WHERE ast_id = %s
                    ORDER BY priority ASC;
                    """,
                    (sec_row["ast_id"],),
                )
                for r in cursor.fetchall():
                    attrs[safe_decode(r["name"])] = safe_decode(r["val"])

            sec_dict = {
                "sec_id": sec_id,
                "name": safe_decode(sec_row["name"]),
                "status": safe_decode(sec_row["status"]),
                "scm_tree": safe_decode(sec_row["scm_tree"]),
                "web_page": safe_decode(sec_row["web_page"]),
                "mailing_list": safe_decode(sec_row["mailing_list"]),
                "patchwork": safe_decode(sec_row.get("patchwork")) or attrs.get("patchwork", ""),
                "bug_url": safe_decode(sec_row.get("bug_url")) or attrs.get("bug_url", ""),
                "chat_channel": safe_decode(sec_row.get("chat_channel")) or attrs.get("chat_channel", ""),
                "profile_doc": safe_decode(sec_row.get("profile_doc")) or attrs.get("profile_doc", ""),
                "members": members,
                "patterns": patterns,
                "files": files,
                "file_count": len(all_files),
                "files_truncated": len(all_files) > len(files),
            }
            return {
                "version": vname,
                "section": sec_dict,
                **sec_dict,
            }

    def get_person_profile(self, version_name: str, person_id_or_email: int | str) -> dict[str, Any]:
        """Fetch developer profile, maintained sections, and CREDITS biographical data."""
        import email.utils
        import urllib.parse
        clean_id = urllib.parse.unquote(str(person_id_or_email)).strip()
        if "%" in clean_id:
            clean_id = urllib.parse.unquote(clean_id).strip()
        stripped_id = clean_id.strip("<>'\" \t\r\n")
        parsed_name, parsed_email = email.utils.parseaddr(clean_id)
        parsed_name = parsed_name.strip("<>'\" \t\r\n")
        parsed_email = parsed_email.strip("<>'\" \t\r\n")

        with get_db_cursor() as cursor:
            if stripped_id.isdigit():
                cursor.execute(
                    "SELECT person_id, name, email FROM m_maintainer_person WHERE person_id = %s LIMIT 1;",
                    (int(stripped_id),),
                )
            else:
                cursor.execute(
                    """
                    SELECT person_id, name, email FROM m_maintainer_person
                    WHERE LOWER(email) = LOWER(%s)
                       OR LOWER(name) = LOWER(%s)
                       OR LOWER(email) = LOWER(%s)
                       OR (LENGTH(%s) > 0 AND LOWER(email) = LOWER(%s))
                       OR (LENGTH(%s) > 0 AND LOWER(name) = LOWER(%s))
                    ORDER BY (name = '') ASC, person_id ASC LIMIT 1;
                    """,
                    (clean_id, clean_id, stripped_id, parsed_email, parsed_email, parsed_name, parsed_name),
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
        """List historical CREDITS entries. An `@domain` query matches the entries' addresses only."""
        target_q = query
        domain = _domain_query(target_q)
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
            if domain:
                in_pids, pid_params = _in_ids("p.person_id", self._domain_person_ids(cursor, domain))
                sql += f" AND {in_pids}"
                params.extend(pid_params)
            elif clean_q:
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

            cols = self._section_select_cols(cursor)
            cursor.execute(
                f"""
                SELECT {cols}
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
                    patchwork=safe_decode(r.get("patchwork")) or "",
                    bug_url=safe_decode(r.get("bug_url")) or "",
                    chat_channel=safe_decode(r.get("chat_channel")) or "",
                    profile_doc=safe_decode(r.get("profile_doc")) or "",
                    sec_id=sec_id,
                )
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

    def sections_for_paths(self, version_name: str, paths: list[str]) -> dict[str, list[MaintainerSection]]:
        """Sections covering each path, in the matcher's order.

        Database first: m_maintainer_file names the sections of each path (Step 6.7), and a matcher over
        only those few sections orders them as the full matcher would (sections match independently).
        Paths without rows (not computed yet, unknown path, a database keyed by MAINTAINERS blob, a
        revision whose MAINTAINERS was never indexed) use the full matcher."""
        clean = list(dict.fromkeys(p.strip().lstrip("/") for p in paths if p and p.strip().lstrip("/")))
        sections, matcher = self.get_maintainer_data(version_name)
        out: dict[str, list[MaintainerSection]] = {}
        stored: dict[str, set[int]] = {}
        if clean and sections:
            ids = [getattr(sec, "sec_id", None) for sec in sections]
            try:
                with get_db_cursor() as cursor:
                    for i in range(0, len(clean), 500):
                        stored.update(self._stored_sections_for_paths(cursor, clean[i : i + 500], [x for x in ids if x]))
            except Exception:  # noqa: BLE001 - the matcher answers every path
                stored = {}
        for path in clean:
            found = stored.get(path)
            if found:
                subset = [sec for sec in sections if getattr(sec, "sec_id", None) in found]
                matched = MaintainerMatcher(subset).match_file(path)
                if matched:
                    out[path] = matched
                    continue
                elif subset:
                    out[path] = subset
                    continue
            out[path] = matcher.match_file(path)
        return out

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
        matched_secs = self.sections_for_paths(version_name, [file_path]).get(file_path.strip().lstrip("/"), [])
        return self._subsystems_result(version_name, file_path, matched_secs)

    def _subsystems_result(self, version_name: str, file_path: str, matched_secs: list[MaintainerSection]) -> list[dict[str, Any]]:
        """Response rows of resolve_subsystems_for_file() for the sections matched_secs (cached)."""
        cache_key = (version_name, file_path)
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
                "patchwork": getattr(sec, "patchwork", "") or "",
                "bug_url": getattr(sec, "bug_url", "") or "",
                "chat_channel": getattr(sec, "chat_channel", "") or "",
                "profile_doc": getattr(sec, "profile_doc", "") or "",
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

        # One database lookup for every file; resolve_subsystems_for_file() then reads its cache
        uncached = [f for f in touched_files if _FILE_SUBSYSTEMS_CACHE.get((vname, f)) is None]
        prefetched = self.sections_for_paths(vname, uncached) if len(uncached) > 1 else {}
        file_breakdown = []
        for fpath in touched_files:
            key = fpath.strip().lstrip("/")
            if key in prefetched:
                subs = _FILE_SUBSYSTEMS_CACHE.get((vname, fpath)) or self._subsystems_result(vname, fpath, prefetched[key])
            else:
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
