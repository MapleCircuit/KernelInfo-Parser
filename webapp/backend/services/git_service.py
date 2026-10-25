"""webapp/backend/services/git_service.py - Throttled Git Blame, Commits & Format-Patch."""
from __future__ import annotations
import asyncio
import logging
import re
import subprocess
from webapp.backend.config import get_repo_dir
from functools import lru_cache
from typing import Any
from fastapi import HTTPException
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import (
    hash_hex,
    safe_decode,
    get_version_info,
)
from webapp.backend.security.jail import resolve_and_verify_repo_path

logger = logging.getLogger(__name__)

_GIT_SEMAPHORE = asyncio.Semaphore(8)


_HEX_REF = re.compile(r"[0-9a-f]{4,40}")
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
MAX_COMMIT_DIFF_BYTES = 8 * 1024 * 1024
MAX_COMMIT_DIFF_LINES = 6000
MAX_DIFF_LINE_CHARS = 400


def _strip_diff_prefix(path: str, prefix: str) -> str:
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    return path[len(prefix):] if path.startswith(prefix) else path


def parse_unified_diff(text: str, max_lines: int = MAX_COMMIT_DIFF_LINES) -> list[dict[str, Any]]:
    """Parse `git diff` output into files -> hunks -> [kind, old_no, new_no, text] rows.

    kind is "+", "-" or " ". Once `max_lines` rows have been stored, later rows are only counted
    (additions/deletions stay exact) and the affected files are flagged `truncated`.
    """
    files: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    hunk: dict[str, Any] | None = None
    stored = 0
    old_no = new_no = 0
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            rest = line[len("diff --git "):]
            # Non-rename headers are "a/P b/P", so P is recoverable even when it contains spaces.
            guess = rest[2: 2 + (len(rest) - 5) // 2] if len(rest) >= 5 else rest
            cur = {"path": guess, "old_path": guess, "status": "M", "additions": 0, "deletions": 0,
                   "binary": False, "truncated": False, "hunks": []}
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        if hunk is None:
            if line.startswith("new file mode"):
                cur["status"] = "A"
            elif line.startswith("deleted file mode"):
                cur["status"] = "D"
            elif line.startswith("rename from "):
                cur["old_path"], cur["status"] = line[len("rename from "):], "R"
            elif line.startswith("rename to "):
                cur["path"] = line[len("rename to "):]
            elif line.startswith("--- ") and line != "--- /dev/null":
                cur["old_path"] = _strip_diff_prefix(line[4:], "a/")
            elif line.startswith("+++ ") and line != "+++ /dev/null":
                cur["path"] = _strip_diff_prefix(line[4:], "b/")
            elif line.startswith("Binary files ") or line == "GIT binary patch":
                cur["binary"] = True
        m = _HUNK_RE.match(line) if line.startswith("@@") else None
        if m:
            old_no, new_no = int(m.group(1)), int(m.group(3))
            hunk = {"old_start": old_no, "old_count": int(m.group(2) or 1), "new_start": new_no,
                    "new_count": int(m.group(4) or 1), "heading": m.group(5), "lines": []}
            cur["hunks"].append(hunk)
            continue
        if hunk is None or not line or line[0] not in "+- ":
            continue
        kind = line[0]
        row = [kind, old_no if kind != "+" else None, new_no if kind != "-" else None, line[1:1 + MAX_DIFF_LINE_CHARS]]
        if kind == "+":
            cur["additions"] += 1
            new_no += 1
        elif kind == "-":
            cur["deletions"] += 1
            old_no += 1
        else:
            old_no += 1
            new_no += 1
        if stored < max_lines:
            hunk["lines"].append(row)
            stored += 1
        else:
            cur["truncated"] = True
    for f in files:
        f["hunks"] = [h for h in f["hunks"] if h["lines"]] if f["truncated"] else f["hunks"]
    return files


def format_commit_hash(val: Any) -> str:
    """Format 20-byte binary commit hash into 40-character hex string."""
    if val is None:
        return ""
    if isinstance(val, (bytes, bytearray, memoryview)):
        b = bytes(val)
        if len(b) == 20:
            return b.hex()
        return safe_decode(b)
    return hash_hex(val) if isinstance(val, str) else str(val)


@lru_cache(maxsize=1024)
def _cached_git_file_history(
    rev: str, rel_path: str, limit: int, repo_dir: str = ""
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Execute git log to fetch commits for rel_path up to rev, cached in memory."""
    args = [
        "log",
        f"-n{max(1, min(limit, 200))}",
        "--format=%H%x00%an%x00%ae%x00%at%x00%s",
        rev,
        "--",
        rel_path,
    ]
    proc = subprocess.run(
        ["git"] + args,
        cwd=repo_dir or str(get_repo_dir()),
        capture_output=True,
        text=True,
        timeout=8.0,
    )
    if proc.returncode != 0:
        return None, []

    commits: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        line_str = line.strip()
        if not line_str:
            continue
        parts = line_str.split("\x00")
        if len(parts) >= 5:
            chash, aname, aemail, adate_str, subj = parts[0], parts[1], parts[2], parts[3], parts[4]
            try:
                adate = int(adate_str)
                from datetime import datetime, timezone
                dt = datetime.fromtimestamp(adate, tz=timezone.utc)
                adate_iso = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                adate = 0
                adate_iso = ""

            commits.append({
                "commit_hash": chash,
                "commit_hash_short": chash[:10],
                "author_name": aname,
                "author_email": aemail,
                "author_date": adate,
                "author_date_iso": adate_iso,
                "subject": subj,
            })

    last_changed = commits[0] if commits else None
    return last_changed, commits


class GitService:
    """Service handling git blame, paginated commit history, and patch formatting."""

    @staticmethod
    def run_git(args: list[str], timeout: float = 12.0) -> tuple[str, str, int]:
        """Execute git CLI command synchronously with timeout guard."""
        proc = subprocess.run(
            ["git"] + args,
            cwd=str(get_repo_dir()),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.stdout, proc.stderr, proc.returncode

    def get_blame(self, version_name: str, file_path: str) -> dict[str, Any]:
        """Retrieve line-by-line git blame annotations with LRU caching."""
        import urllib.parse
        clean_file_path = urllib.parse.unquote(str(file_path).strip()).strip("/")
        target_path = resolve_and_verify_repo_path(clean_file_path)
        rel_path = str(target_path.relative_to(get_repo_dir()))

        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
        rev = vname or str(version_name).strip()

        try:
            stdout, stderr, code = self.run_git(["blame", "--line-porcelain", rev, "--", rel_path])
            if code != 0:
                raise HTTPException(status_code=404, detail=f"Git blame failed: {stderr.strip() or 'Unknown error'}")

            lines = stdout.splitlines()
            entries: list[dict[str, Any]] = []
            curr: dict[str, Any] = {}
            for line in lines:
                if line.startswith("\t"):
                    curr["line_content"] = line[1:]
                    final_l = curr.get("final_line", 1)
                    orig_l = curr.get("orig_line", 1)
                    author = curr.get("author", "Unknown")
                    author_mail = curr.get("author_mail", "")
                    author_time = curr.get("author_time", 0)
                    summary = curr.get("summary", "")

                    curr["line"] = final_l
                    curr["final_line"] = final_l
                    curr["orig_line"] = orig_l
                    curr["author_name"] = author
                    curr["author"] = author
                    curr["author_email"] = author_mail
                    curr["author_mail"] = author_mail
                    curr["date"] = author_time
                    curr["author_time"] = author_time
                    curr["subject"] = summary
                    curr["summary"] = summary

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
                "version": vname or version_name,
                "vid": vid,
                "path": rel_path,
                "file_path": rel_path,
                "total_lines": len(entries),
                "lines": entries,
                "blame": entries,
            }
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="Git blame operation timed out.")

    def get_file_last_changed(self, version_name: str, file_path: str) -> dict[str, Any] | None:
        """Retrieve the most recent git commit modifying file_path up to version_name."""
        try:
            history = self.get_file_history(version_name, file_path, limit=1)
            return history.get("last_changed")
        except Exception:
            return None

    def get_file_history(
        self, version_name: str, file_path: str, limit: int = 50
    ) -> dict[str, Any]:
        """Retrieve chronological git commit history and last changed metadata for a specific file."""
        import urllib.parse
        clean_file_path = urllib.parse.unquote(str(file_path).strip()).strip("/")
        target_path = resolve_and_verify_repo_path(clean_file_path)
        rel_path = str(target_path.relative_to(get_repo_dir()))

        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
        rev = vname or str(version_name).strip()

        try:
            last_changed, commits = _cached_git_file_history(rev, rel_path, max(1, min(limit, 200)), str(get_repo_dir()))
        except subprocess.TimeoutExpired:
            raise HTTPException(
                status_code=504,
                detail="Git history operation timed out.",
            )

        return {
            "version": str(version_name).strip(),
            "path": rel_path,
            "total_commits": len(commits),
            "last_changed": last_changed,
            "commits": commits,
        }



    def format_patch(self, version_name: str, req: Any) -> dict[str, Any]:
        """Generate an RFC-2822 compliant patch string."""
        import difflib
        from datetime import datetime, timezone
        from webapp.backend.models import FormatPatchRequest

        if isinstance(req, dict):
            req = FormatPatchRequest(**req)

        orig_lines = req.original_content.splitlines()
        mod_lines = req.modified_content.splitlines()
        file_path = req.file_path.strip().lstrip("/")

        hunks = list(difflib.unified_diff(
            orig_lines,
            mod_lines,
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            lineterm="",
        ))
        insertions = sum(1 for ln in hunks[2:] if ln.startswith("+"))
        deletions = sum(1 for ln in hunks[2:] if ln.startswith("-"))
        diff_text = "\n".join([f"diff --git a/{file_path} b/{file_path}", *hunks]) if hunks else ""

        subj = req.commit_subject.strip()
        if not subj.startswith("[PATCH]"):
            subj = f"[PATCH] {subj}"

        timestamp = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
        author_name = getattr(req, "author_name", "Kernel Developer")
        author_email = getattr(req, "author_email", "dev@kernel.org")
        body = (getattr(req, "commit_body", "") or "").strip() or f"Update {file_path}."
        changes = insertions + deletions
        bar = "+" * insertions + "-" * deletions
        if changes > 60:
            bar = "+" * round(60 * insertions / changes) + "-" * round(60 * deletions / changes)
        summary = ", ".join(
            part for part in (
                "1 file changed",
                f"{insertions} insertion{'s' if insertions != 1 else ''}(+)" if insertions else "",
                f"{deletions} deletion{'s' if deletions != 1 else ''}(-)" if deletions else "",
            ) if part
        )

        formatted = (
            f"From: {author_name} <{author_email}>\n"
            f"Date: {timestamp}\n"
            f"Subject: {subj}\n\n"
            f"{body}\n\n"
            f"Signed-off-by: {author_name} <{author_email}>\n"
            f"---\n"
            f" {file_path} | {changes} {bar}\n"
            f" {summary}\n\n"
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
                       (SELECT COUNT(*) FROM m_commit_file cf WHERE cf.commit_id = c.commit_id) AS files_count
                FROM m_commit c
                JOIN m_maintainer_person p ON c.author_id = p.person_id
                WHERE c.commit_id > 0
            """
            params: list[Any] = []
            if query:
                # commit_hash is BINARY(20): match hex prefixes against its HEX() rendering
                sql += " AND (c.subject LIKE %s OR HEX(c.commit_hash) LIKE %s OR p.name LIKE %s)"
                params.extend([f"%{query}%", f"{query.strip().upper()}%", f"%{query}%"])
            sql += " ORDER BY c.author_date DESC LIMIT %s OFFSET %s;"
            params.extend([limit, off])

            cursor.execute(sql, tuple(params))
            commits = [
                {
                    "commit_id": r["commit_id"],
                    "commit_hash": format_commit_hash(r["commit_hash"]),
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
                cid_str = str(commit_id).strip()
                if len(cid_str) == 40:
                    try:
                        c_bytes = bytes.fromhex(cid_str)
                    except ValueError:
                        c_bytes = cid_str.encode("latin-1")
                else:
                    c_bytes = cid_str.encode("latin-1")
                cursor.execute("SELECT * FROM m_commit WHERE commit_hash = %s LIMIT 1;", (c_bytes,))
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
                SELECT cf.change_type, fn.fname
                FROM m_commit_file cf
                JOIN m_file_name fn ON cf.fnid = fn.fnid
                WHERE cf.commit_id = %s;
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
                "commit_hash": format_commit_hash(c_row["commit_hash"]),
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

    def get_commit_diff(self, commit: str, context: int = 3) -> dict[str, Any]:
        """Structured unified diff of one commit against its first parent (root commits diff against the empty tree)."""
        ref = (commit or "").strip().lower()
        if not _HEX_REF.fullmatch(ref):
            raise HTTPException(status_code=400, detail="A commit hash (4-40 hex digits) must be specified.")
        context = max(0, min(int(context), 20))
        try:
            out, _, rc = self.run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], timeout=10)
            if rc != 0:
                raise HTTPException(status_code=404, detail=f"Commit '{commit}' not found in Git.")
            sha = out.strip()
            out, _, _ = self.run_git(["rev-list", "--parents", "-n1", sha], timeout=10)
            parents = out.split()[1:]
            base = parents[0] if parents else _EMPTY_TREE
            # Stream stdout and stop reading at the byte cap: a tree-import commit can diff to hundreds of MB.
            with subprocess.Popen(
                ["git", "-c", "core.quotePath=false", "diff", "--no-color", "--no-ext-diff", "-M", f"-U{context}", base, sha],
                cwd=str(get_repo_dir()),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            ) as proc:
                raw = proc.stdout.read(MAX_COMMIT_DIFF_BYTES + 1)
                truncated = len(raw) > MAX_COMMIT_DIFF_BYTES
                if truncated:
                    proc.kill()
                    raw = raw[: raw.rfind(b"\n", 0, MAX_COMMIT_DIFF_BYTES) + 1]
                rc = proc.wait(timeout=30)
        except subprocess.TimeoutExpired as e:
            raise HTTPException(status_code=504, detail="git diff timed out.") from e
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"git diff failed: {e}") from e
        if rc != 0 and not truncated:
            raise HTTPException(status_code=500, detail="git diff failed")

        files = parse_unified_diff(raw.decode("utf-8", errors="replace"), MAX_COMMIT_DIFF_LINES)
        return {
            "commit_hash": sha,
            "parent_hash": parents[0] if parents else None,
            "is_merge": len(parents) > 1,
            "context": context,
            "files": files,
            "additions": sum(f["additions"] for f in files),
            "deletions": sum(f["deletions"] for f in files),
            "truncated": truncated or any(f["truncated"] for f in files),
        }

    def get_file_blame(self, version_name: str, fid_or_path: int | str) -> dict[str, Any]:
        """Retrieve tag-by-tag commit provenance and git blame annotations."""
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            blob_id = 0
            file_path = ""
            target_id = 0
            if isinstance(fid_or_path, int) or str(fid_or_path).isdigit():
                target_id = int(fid_or_path)
                cursor.execute(
                    """
                    SELECT fn.fname, bcp.blob_id
                    FROM m_bridge_checkpoint bcp
                    JOIN m_file_name fn ON bcp.fnid = fn.fnid
                    WHERE bcp.blob_id = %s
                    LIMIT 1;
                    """,
                    (target_id,),
                )
                r = cursor.fetchone()
                if r:
                    file_path = safe_decode(r["fname"])
                    blob_id = r["blob_id"]
                else:
                    cursor.execute(
                        """
                        SELECT fname FROM m_file_name WHERE fnid = %s LIMIT 1;
                        """,
                        (target_id,),
                    )
                    r = cursor.fetchone()
                    if r:
                        file_path = safe_decode(r["fname"])
                        blob_id = target_id
                    else:
                        blob_id = target_id
            else:
                file_path = str(fid_or_path)
                cursor.execute(
                    """
                    SELECT bcp.blob_id
                    FROM m_file_name fn
                    JOIN m_bridge_checkpoint bcp ON fn.fnid = bcp.fnid
                    WHERE fn.fname = %s
                    LIMIT 1;
                    """,
                    (file_path,),
                )
                r = cursor.fetchone()
                blob_id = r["blob_id"] if r else 0

            # Tags blame
            cursor.execute(
                """
                SELECT bt.tag_id, bt.line_s, bt.line_e, bt.char_s, bt.char_e
                FROM m_bridge_tag bt
                WHERE bt.blob_id = %s
                ORDER BY bt.line_s ASC
                LIMIT 200;
                """,
                (blob_id,),
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

        result: dict[str, Any] = {
            "fid": target_id if (isinstance(fid_or_path, int) or str(fid_or_path).isdigit()) else blob_id,
            "blob_id": blob_id,
            "version": vname,
            "path": file_path,
            "total_tags": len(tags),
            "tags": tags,
            "total_lines": 0,
            "lines": [],
            "blame": [],
        }

        if file_path:
            try:
                b_res = self.get_blame(vname or version_name, file_path)
                result["total_lines"] = b_res.get("total_lines", 0)
                result["lines"] = b_res.get("lines", [])
                result["blame"] = b_res.get("blame", [])
            except Exception as e:
                logger.debug("Could not attach git blame to get_file_blame: %s", e)

        return result


    def get_commit_timeline(
        self,
        version_name: str,
        limit: int = 100,
        direction: str = "past",
        cursor_id: int | None = None,
    ) -> dict[str, Any]:
        """Retrieve structured commit timeline starting from the version tag anchor, supporting past and future exploration."""
        import datetime

        def _format_date(ts: int) -> str:
            try:
                return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
            except Exception:
                return ""

        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                vid, vname = get_version_info(cursor, "v3.0")

            anchor_row = None
            if vid and vid > 0:
                cursor.execute(
                    """
                    SELECT c.commit_id, c.commit_hash, c.author_date, c.subject,
                           p.person_id AS author_id, p.name AS author_name, p.email AS author_email
                    FROM m_commit c
                    JOIN m_maintainer_person p ON c.author_id = p.person_id
                    WHERE c.commit_id = %s
                    LIMIT 1;
                    """,
                    (vid,),
                )
                anchor_row = cursor.fetchone()

            if not anchor_row:
                # Find closest commit or latest tag commit
                cursor.execute(
                    """
                    SELECT c.commit_id, c.commit_hash, c.author_date, c.subject,
                           p.person_id AS author_id, p.name AS author_name, p.email AS author_email
                    FROM m_commit c
                    JOIN m_maintainer_person p ON c.author_id = p.person_id
                    WHERE c.commit_id > 0
                    ORDER BY c.commit_id DESC
                    LIMIT 1;
                    """
                )
                anchor_row = cursor.fetchone()

            if not anchor_row:
                return {
                    "version": version_name,
                    "direction": direction,
                    "anchor_commit": {},
                    "total_commits": 0,
                    "displayed_commits": 0,
                    "top_contributors": [],
                    "timeline": [],
                }

            anchor_id = anchor_row["commit_id"]
            anchor_hash = format_commit_hash(anchor_row["commit_hash"])
            anchor_subject = safe_decode(anchor_row["subject"])
            anchor_author = safe_decode(anchor_row["author_name"])

            raw_rows: list[dict[str, Any]] = []
            dir_mode = (direction or "past").lower().strip()

            if dir_mode == "future":
                start_id = cursor_id if cursor_id is not None else anchor_id
                cursor.execute(
                    """
                    SELECT c.commit_id, c.commit_hash, c.author_date, c.subject,
                           p.person_id AS author_id, p.name AS author_name, p.email AS author_email,
                           (SELECT COUNT(*) FROM m_commit_file cf WHERE cf.commit_id = c.commit_id) AS files_count
                    FROM m_commit c
                    JOIN m_maintainer_person p ON c.author_id = p.person_id
                    WHERE c.commit_id >= %s
                    ORDER BY c.commit_id ASC
                    LIMIT %s;
                    """,
                    (start_id, max(1, min(limit, 250))),
                )
                raw_rows = cursor.fetchall()
            elif dir_mode == "around":
                future_limit = max(5, limit // 4)
                past_limit = limit - future_limit
                cursor.execute(
                    """
                    SELECT c.commit_id, c.commit_hash, c.author_date, c.subject,
                           p.person_id AS author_id, p.name AS author_name, p.email AS author_email,
                           (SELECT COUNT(*) FROM m_commit_file cf WHERE cf.commit_id = c.commit_id) AS files_count
                    FROM m_commit c
                    JOIN m_maintainer_person p ON c.author_id = p.person_id
                    WHERE c.commit_id > %s
                    ORDER BY c.commit_id ASC
                    LIMIT %s;
                    """,
                    (anchor_id, future_limit),
                )
                future_rows = list(reversed(cursor.fetchall()))
                cursor.execute(
                    """
                    SELECT c.commit_id, c.commit_hash, c.author_date, c.subject,
                           p.person_id AS author_id, p.name AS author_name, p.email AS author_email,
                           (SELECT COUNT(*) FROM m_commit_file cf WHERE cf.commit_id = c.commit_id) AS files_count
                    FROM m_commit c
                    JOIN m_maintainer_person p ON c.author_id = p.person_id
                    WHERE c.commit_id <= %s
                    ORDER BY c.commit_id DESC
                    LIMIT %s;
                    """,
                    (anchor_id, past_limit),
                )
                past_rows = cursor.fetchall()
                raw_rows = future_rows + past_rows
            else:  # default 'past'
                start_id = cursor_id if cursor_id is not None else anchor_id
                cursor.execute(
                    """
                    SELECT c.commit_id, c.commit_hash, c.author_date, c.subject,
                           p.person_id AS author_id, p.name AS author_name, p.email AS author_email,
                           (SELECT COUNT(*) FROM m_commit_file cf WHERE cf.commit_id = c.commit_id) AS files_count
                    FROM m_commit c
                    JOIN m_maintainer_person p ON c.author_id = p.person_id
                    WHERE c.commit_id <= %s
                    ORDER BY c.commit_id DESC
                    LIMIT %s;
                    """,
                    (start_id, max(1, min(limit, 250))),
                )
                raw_rows = cursor.fetchall()

            commits = []
            author_counts: dict[str, dict[str, Any]] = {}
            for r in raw_rows:
                cid = r["commit_id"]
                name = safe_decode(r["author_name"]) or "Unknown"
                email = safe_decode(r["author_email"]) or ""
                key = email or name
                if key not in author_counts:
                    author_counts[key] = {"name": name, "email": email, "commits_count": 0}
                author_counts[key]["commits_count"] += 1

                commits.append({
                    "commit_id": cid,
                    "commit_hash": format_commit_hash(r["commit_hash"]),
                    "author_date": r["author_date"],
                    "author_date_iso": _format_date(r["author_date"]),
                    "subject": safe_decode(r["subject"]),
                    "author": {
                        "person_id": r["author_id"],
                        "name": name,
                        "email": email,
                    },
                    "author_name": name,
                    "author_email": email,
                    "files_count": int(r.get("files_count", 0)),
                    "is_anchor": (cid == anchor_id),
                })

            top_contrib = sorted(author_counts.values(), key=lambda x: x["commits_count"], reverse=True)[:10]

            min_id = min((c["commit_id"] for c in commits), default=anchor_id)
            max_id = max((c["commit_id"] for c in commits), default=anchor_id)

            cursor.execute("SELECT 1 FROM m_commit WHERE commit_id < %s AND commit_id > 0 LIMIT 1;", (min_id,))
            has_past = cursor.fetchone() is not None
            cursor.execute("SELECT 1 FROM m_commit WHERE commit_id > %s LIMIT 1;", (max_id,))
            has_future = cursor.fetchone() is not None

            return {
                "version": vname,
                "direction": dir_mode,
                "anchor_commit": {
                    "commit_id": anchor_id,
                    "commit_hash": anchor_hash,
                    "subject": anchor_subject,
                    "author_name": anchor_author,
                    "author_date": anchor_row["author_date"],
                    "author_date_iso": _format_date(anchor_row["author_date"]),
                },
                "total_commits": len(commits),
                "displayed_commits": len(commits),
                "has_past": has_past,
                "has_future": has_future,
                "next_past_cursor": max(1, min_id - 1),
                "next_future_cursor": max_id + 1,
                "top_contributors": top_contrib,
                "timeline": commits,
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

