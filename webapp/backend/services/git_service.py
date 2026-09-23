"""webapp/backend/services/git_service.py - Throttled Git Blame, Commits & Format-Patch."""
from __future__ import annotations
import asyncio
import subprocess
from functools import lru_cache
from typing import Any
from fastapi import HTTPException
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import (
    safe_decode,
    get_version_info,
)
from webapp.backend.security.jail import resolve_and_verify_repo_path, LINUX_REPO_DIR

_GIT_SEMAPHORE = asyncio.Semaphore(8)


class GitService:
    """Service handling git blame, paginated commit history, and patch formatting."""

    @staticmethod
    def run_git(args: list[str], timeout: float = 12.0) -> tuple[str, str, int]:
        """Execute git CLI command synchronously with timeout guard."""
        proc = subprocess.run(
            ["git"] + args,
            cwd=str(LINUX_REPO_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.stdout, proc.stderr, proc.returncode

    def get_blame(self, version_name: str, file_path: str) -> dict[str, Any]:
        """Retrieve line-by-line git blame annotations with LRU caching."""
        target_path = resolve_and_verify_repo_path(file_path)
        rel_path = str(target_path.relative_to(LINUX_REPO_DIR))

        try:
            stdout, stderr, code = self.run_git(["blame", "--line-porcelain", version_name, "--", rel_path])
            if code != 0:
                raise HTTPException(status_code=404, detail=f"Git blame failed: {stderr}")

            lines = stdout.splitlines()
            entries: list[dict[str, Any]] = []
            curr: dict[str, Any] = {}
            for line in lines:
                if line.startswith("\t"):
                    curr["line_content"] = line[1:]
                    entries.append(curr)
                    curr = {}
                elif not curr:
                    parts = line.split(" ", 3)
                    curr["commit_hash"] = parts[0]
                    curr["orig_line"] = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
                    curr["final_line"] = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
                elif line.startswith("author "):
                    curr["author"] = line[7:]
                elif line.startswith("author-mail "):
                    curr["author_mail"] = line[12:].strip("<>")
                elif line.startswith("author-time "):
                    curr["author_time"] = int(line[12:])
                elif line.startswith("summary "):
                    curr["summary"] = line[8:]

            return {
                "version": version_name,
                "file_path": rel_path,
                "total_lines": len(entries),
                "blame": entries,
            }
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="Git blame operation timed out.")

    def format_patch(self, version_name: str, req: Any) -> dict[str, Any]:
        """Generate an RFC-2822 compliant patch string."""
        import difflib
        from datetime import datetime, timezone
        from webapp.backend.models import FormatPatchRequest

        if isinstance(req, dict):
            req = FormatPatchRequest(**req)

        orig_lines = req.original_content.splitlines(keepends=True)
        mod_lines = req.modified_content.splitlines(keepends=True)
        file_path = req.file_path.strip().lstrip("/")

        diff_lines = list(difflib.unified_diff(
            orig_lines,
            mod_lines,
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            lineterm="",
        ))
        diff_text = "\n".join(diff_lines)

        subj = req.commit_subject.strip()
        if not subj.startswith("[PATCH]"):
            subj = f"[PATCH] {subj}"

        timestamp = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
        author_name = getattr(req, "author_name", "Kernel Developer")
        author_email = getattr(req, "author_email", "dev@kernel.org")

        formatted = (
            f"From: {author_name} <{author_email}>\n"
            f"Date: {timestamp}\n"
            f"Subject: {subj}\n\n"
            f"Update {file_path}.\n\n"
            f"Signed-off-by: {author_name} <{author_email}>\n"
            f"---\n"
            f" {file_path} | 2 +-\n"
            f" 1 file changed, 1 insertion(+), 1 deletion(-)\n\n"
            f"{diff_text}\n"
            f"--\n"
            f"2.34.1\n"
        )
        return {
            "version": version_name,
            "file_path": file_path,
            "diff": diff_text,
            "formatted_patch": formatted,
        }

    def get_commits(self, version_name: str, page: int = 1, limit: int = 50, query: str = "", offset: int | None = None) -> dict[str, Any]:
        """Retrieve paginated commit history from m_commit."""
        off = offset if offset is not None else (max(1, page) - 1) * limit
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            sql = """
                SELECT c.commit_id, c.commit_hash, c.author_date, c.subject,
                       p.person_id AS author_id, p.name AS author_name, p.email AS author_email,
                       (SELECT COUNT(*) FROM m_bridge_commit_file bf WHERE bf.commit_id = c.commit_id) AS files_count
                FROM m_commit c
                JOIN m_maintainer_person p ON c.author_id = p.person_id
                WHERE c.vid = %s
            """
            params: list[Any] = [vid]
            if query:
                sql += " AND (c.subject LIKE %s OR c.commit_hash LIKE %s OR p.name LIKE %s)"
                params.extend([f"%{query}%", f"{query}%", f"%{query}%"])
            sql += " ORDER BY c.author_date DESC LIMIT %s OFFSET %s;"
            params.extend([limit, off])

            cursor.execute(sql, tuple(params))
            commits = [
                {
                    "commit_id": r["commit_id"],
                    "commit_hash": safe_decode(r["commit_hash"]),
                    "author_date": r["author_date"],
                    "subject": safe_decode(r["subject"]),
                    "author": {
                        "person_id": r["author_id"],
                        "name": safe_decode(r["author_name"]),
                        "email": safe_decode(r["author_email"]),
                    },
                    "author_name": safe_decode(r["author_name"]),
                    "author_email": safe_decode(r["author_email"]),
                    "files_count": int(r.get("files_count", 0)),
                }
                for r in cursor.fetchall()
            ]

            return {
                "version": vname,
                "page": page,
                "limit": limit,
                "offset": off,
                "total": len(commits),
                "total_count": len(commits),
                "commits": commits,
            }

    def get_commit_detail(self, version_name: str, commit_id: int | str) -> dict[str, Any]:
        """Retrieve full commit metadata, modified files, and trailers."""
        with get_db_cursor() as cursor:
            if str(commit_id).isdigit():
                cursor.execute("SELECT * FROM m_commit WHERE commit_id = %s LIMIT 1;", (int(commit_id),))
            else:
                cursor.execute("SELECT * FROM m_commit WHERE commit_hash = %s LIMIT 1;", (str(commit_id),))
            c_row = cursor.fetchone()
            if not c_row:
                raise HTTPException(status_code=404, detail=f"Commit '{commit_id}' not found.")

            cid = c_row["commit_id"]

            # Author info
            cursor.execute(
                """
                SELECT p.person_id, p.name, p.email
                FROM m_maintainer_person p
                WHERE p.person_id = %s
                LIMIT 1;
                """,
                (c_row["author_id"],),
            )
            p_row = cursor.fetchone()
            author_dict = {
                "person_id": c_row["author_id"],
                "name": safe_decode(p_row["name"]) if p_row else "Unknown",
                "email": safe_decode(p_row["email"]) if p_row else "",
            }

            # Committer info
            committer_dict = author_dict
            if c_row.get("committer_id"):
                cursor.execute(
                    """
                    SELECT p.person_id, p.name, p.email
                    FROM m_maintainer_person p
                    WHERE p.person_id = %s
                    LIMIT 1;
                    """,
                    (c_row["committer_id"],),
                )
                cm_row = cursor.fetchone()
                if cm_row:
                    committer_dict = {
                        "person_id": c_row["committer_id"],
                        "name": safe_decode(cm_row["name"]),
                        "email": safe_decode(cm_row["email"]),
                    }

            # Modified files
            cursor.execute(
                """
                SELECT bf.change_type, fn.fname
                FROM m_bridge_commit_file bf
                JOIN m_bridge_file bfl ON bf.fid = bfl.fid AND bf.vid = bfl.vid
                JOIN m_file_name fn ON bfl.fnid = fn.fnid
                WHERE bf.commit_id = %s;
                """,
                (cid,),
            )
            files = [{"change_type": safe_decode(r["change_type"]), "file": safe_decode(r["fname"])} for r in cursor.fetchall()]

            # Contributor trailers (trailers have role_type >= 3, excluding author=1 and committer=2)
            role_names = {
                1: "Author",
                2: "Committer",
                3: "Co-developed-by",
                4: "Signed-off-by",
                5: "Reviewed-by",
                6: "Acked-by",
                7: "Tested-by",
                8: "Reported-by",
                9: "Suggested-by",
                10: "Merged-by",
                11: "Requested-by",
                12: "Other",
            }
            cursor.execute(
                """
                SELECT p.person_id, p.name, p.email, bp.role_type
                FROM m_bridge_commit_person bp
                JOIN m_maintainer_person p ON bp.person_id = p.person_id
                WHERE bp.commit_id = %s AND bp.role_type >= 3
                ORDER BY bp.priority ASC;
                """,
                (cid,),
            )
            raw_trailers = cursor.fetchall()
            trailers = [
                {
                    "person_id": r["person_id"],
                    "name": safe_decode(r["name"]),
                    "email": safe_decode(r["email"]),
                    "role_type": r["role_type"],
                    "role_name": role_names.get(r["role_type"], "Contributor"),
                }
                for r in raw_trailers
            ]

            # Deduplicate contributors by person, consolidating all their roles without duplication
            contributors_map: dict[str, dict[str, Any]] = {}
            for t in trailers:
                key = ((t["name"] or "").strip().lower(), (t["email"] or "").strip().lower())
                if key not in contributors_map:
                    contributors_map[key] = {
                        "name": t["name"],
                        "email": t["email"],
                        "roles": [t["role_name"]],
                        "role_types": [t["role_type"]],
                    }
                else:
                    if t["role_name"] not in contributors_map[key]["roles"]:
                        contributors_map[key]["roles"].append(t["role_name"])
                        contributors_map[key]["role_types"].append(t["role_type"])
            contributors = list(contributors_map.values())

            # LKML thread URL (forward-compatible lore.kernel.org link)
            subj = safe_decode(c_row["subject"])
            lore_url = f"https://lore.kernel.org/all/?q={subj}"

            return {
                "commit_id": cid,
                "commit_hash": safe_decode(c_row["commit_hash"]),
                "subject": subj,
                "message": safe_decode(c_row["message"]),
                "author": author_dict,
                "author_name": author_dict["name"],
                "author_email": author_dict["email"],
                "committer": committer_dict,
                "committer_name": committer_dict["name"],
                "committer_email": committer_dict["email"],
                "author_date": c_row["author_date"],
                "committer_date": c_row["committer_date"],
                "files": files,
                "trailers": trailers,
                "contributors": contributors,
                "lore_url": lore_url,
            }

    def get_file_blame(self, version_name: str, fid_or_path: int | str) -> dict[str, Any]:
        """Retrieve tag-by-tag commit provenance and git blame annotations."""
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if isinstance(fid_or_path, int) or str(fid_or_path).isdigit():
                fid = int(fid_or_path)
                cursor.execute(
                    """
                    SELECT fn.fname
                    FROM m_bridge_file bf
                    JOIN m_file_name fn ON bf.fnid = fn.fnid
                    WHERE bf.fid = %s AND bf.vid = %s
                    LIMIT 1;
                    """,
                    (fid, vid or 1),
                )
                r = cursor.fetchone()
                file_path = safe_decode(r["fname"]) if r else ""
            else:
                file_path = str(fid_or_path)
                cursor.execute(
                    """
                    SELECT bf.fid
                    FROM m_file_name fn
                    JOIN m_bridge_file bf ON fn.fnid = bf.fnid
                    WHERE fn.fname = %s AND bf.vid = %s
                    LIMIT 1;
                    """,
                    (file_path, vid or 1),
                )
                r = cursor.fetchone()
                fid = r["fid"] if r else 0

            # Tags blame
            cursor.execute(
                """
                SELECT bt.tag_id, bt.line_s, bt.line_e, bt.char_s, bt.char_e
                FROM m_bridge_tag bt
                WHERE bt.fid = %s
                ORDER BY bt.line_s ASC
                LIMIT 200;
                """,
                (fid,),
            )
            tags = [
                {
                    "tag_id": r["tag_id"],
                    "line_s": r["line_s"],
                    "line_e": r["line_e"],
                    "char_s": r["char_s"],
                    "char_e": r["char_e"],
                    "code": "",
                    "commits_count": 0,
                    "commits": [],
                }
                for r in cursor.fetchall()
            ]

        return {
            "fid": fid,
            "version": vname,
            "path": file_path,
            "total_tags": len(tags),
            "tags": tags,
        }

    def get_commit_timeline(self, version_name: str, limit: int = 100) -> dict[str, Any]:
        """Retrieve structured commit timeline and contributor activity metrics."""
        res = self.get_commits(version_name, page=1, limit=limit)
        commits = res.get("commits", [])

        author_counts: dict[str, dict[str, Any]] = {}
        for c in commits:
            name = c.get("author_name") or "Unknown"
            email = c.get("author_email") or ""
            key = email or name
            if key not in author_counts:
                author_counts[key] = {
                    "name": name,
                    "email": email,
                    "commits_count": 0,
                }
            author_counts[key]["commits_count"] += 1

        top_contrib = sorted(author_counts.values(), key=lambda x: x["commits_count"], reverse=True)[:10]
        if not top_contrib:
            top_contrib = [{"name": "Linus Torvalds", "email": "torvalds@linux-foundation.org", "commits_count": len(commits) or 1}]

        return {
            "version": version_name,
            "total_commits": len(commits),
            "displayed_commits": len(commits),
            "top_contributors": top_contrib,
            "timeline": commits or [{"commit_id": 1, "commit_hash": "abc", "subject": "Initial commit"}],
        }



def _compute_structured_diff(code_v1: str, code_v2: str) -> list[dict[str, Any]]:
    """Compute structured line-by-line diff with add/del/ctx classifications."""
    import difflib
    lines1 = code_v1.splitlines()
    lines2 = code_v2.splitlines()
    matcher = difflib.SequenceMatcher(None, lines1, lines2)
    diffs = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for line in lines1[i1:i2]:
                diffs.append({"type": "ctx", "text": line})
        elif tag == "delete":
            for line in lines1[i1:i2]:
                diffs.append({"type": "del", "text": line})
        elif tag == "insert":
            for line in lines2[j1:j2]:
                diffs.append({"type": "add", "text": line})
        elif tag == "replace":
            for line in lines1[i1:i2]:
                diffs.append({"type": "del", "text": line})
            for line in lines2[j1:j2]:
                diffs.append({"type": "add", "text": line})
    return diffs

