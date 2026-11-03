"""webapp/backend/services/git_service.py - Git Blame, Commits, Timelines & Format-Patch."""
from __future__ import annotations
import logging
import re
import subprocess
import time
import urllib.parse
from webapp.backend.config import get_repo_dir
from functools import lru_cache
from typing import Any
from fastapi import HTTPException
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import (
    commit_prefix_range,
    get_active_network_id,
    hash_hex,
    safe_decode,
    get_version_info,
)
from webapp.backend.security.git import is_safe_rev, safe_rev
from webapp.backend.security.sql import sanitize_like_query
from webapp.backend.security.jail import resolve_and_verify_repo_path
from webapp.backend.services.mailing_list_service import LORE_BASE_URL

logger = logging.getLogger(__name__)

_HEX_REF = re.compile(r"[0-9a-f]{4,40}")
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
MAX_COMMIT_PAGE = 500
# A deeper page than this is a scan, not navigation: MySQL still walks every skipped row
MAX_COMMIT_OFFSET = 1_000_000
MAX_COMMIT_DIFF_BYTES = 8 * 1024 * 1024
MAX_COMMIT_DIFF_LINES = 6000
MAX_DIFF_LINE_CHARS = 400
# Cap on a commit file list resolved live from Git (a merge of a long branch touches thousands)
MAX_COMMIT_FILE_ROWS = 5000


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
    # rev reaches the command line before "--", where git would read "--output=..." as an option
    if not is_safe_rev(rev):
        return None, []
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
        """Line-by-line `git blame` of a file at a revision: the commit, author, date and subject of each line."""
        import urllib.parse
        clean_file_path = urllib.parse.unquote(str(file_path).strip()).strip("/")
        target_path = resolve_and_verify_repo_path(clean_file_path)
        rel_path = str(target_path.relative_to(get_repo_dir()))

        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
        # An unresolved version falls back to the raw name, which git would read as an option
        rev = safe_rev(vname or str(version_name).strip(), "version")

        try:
            # --porcelain names each commit's author and summary once, on its first line only
            stdout, stderr, code = self.run_git(["blame", "--porcelain", rev, "--", rel_path])
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="Git blame operation timed out.") from None
        if code != 0:
            raise HTTPException(status_code=404, detail=f"Git blame failed: {stderr.strip() or 'Unknown error'}")

        commits: dict[str, dict[str, Any]] = {}
        entries: list[dict[str, Any]] = []
        info: dict[str, Any] | None = None
        sha = ""
        final_line = 1
        for raw_line in stdout.split("\n"):
            line = raw_line.rstrip("\r")
            if not line:
                continue
            if line.startswith("\t"):
                entries.append({
                    "line": final_line,
                    "commit_hash": sha,
                    "author_name": info.get("author", "Unknown") if info else "Unknown",
                    "author_email": info.get("author_mail", "") if info else "",
                    "date": info.get("author_time", 0) if info else 0,
                    "subject": info.get("summary", "") if info else "",
                })
                info = None
                continue
            if info is None:
                # Line header: "<sha> <orig_line> <final_line> [<lines in group>]"
                parts = line.split(" ")
                if len(parts) >= 3 and parts[2].isdigit():
                    sha = parts[0]
                    final_line = int(parts[2])
                    info = commits.setdefault(sha, {})
            elif line.startswith("author "):
                info["author"] = line[7:]
            elif line.startswith("author-mail "):
                info["author_mail"] = line[12:].strip("<>")
            elif line.startswith("author-time "):
                info["author_time"] = int(line[12:]) if line[12:].isdigit() else 0
            elif line.startswith("summary "):
                info["summary"] = line[8:]

        return {
            "version": vname or version_name,
            "vid": vid,
            "path": rel_path,
            "lines": entries,
        }

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
        rev = safe_rev(vname or str(version_name).strip(), "version")

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

    def get_commits(self, version_name: str, page: int = 1, limit: int = 50, query: str = "") -> dict[str, Any]:
        """Search the indexed commits of the request's repository (its Git network), newest first."""
        # limit and the page's offset are bound straight into SQL LIMIT/OFFSET
        limit = max(1, min(int(limit), MAX_COMMIT_PAGE))
        off = max(0, min((max(1, int(page)) - 1) * limit, MAX_COMMIT_OFFSET))
        now_ts = int(time.time())
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")
            network_id = get_active_network_id(cursor)
            if network_id is None:
                return {"version": vname, "page": page, "limit": limit, "offset": off, "total": 0, "commits": []}

            commits: list[dict[str, Any]] = []
            q_clean = query.strip()

            # Case 1: Query is a commit hash or prefix (4-40 hex characters) -> indexed binary lookup
            if q_clean and _HEX_REF.fullmatch(q_clean.lower()):
                lo, hi = commit_prefix_range(q_clean)
                if hi:
                    sql = """
                        SELECT c.commit_id, c.commit_hash, c.author_date, c.subject,
                               p.person_id AS author_id, p.name AS author_name, p.email AS author_email,
                               (SELECT COUNT(*) FROM m_commit_file cf WHERE cf.commit_id = c.commit_id) AS files_count
                        FROM m_commit c
                        JOIN m_maintainer_person p ON c.author_id = p.person_id
                        WHERE c.commit_id > 0 AND c.network_id = %s
                          AND c.commit_hash >= %s AND c.commit_hash < %s
                        ORDER BY c.commit_id DESC LIMIT %s OFFSET %s;
                    """
                    cursor.execute(sql, (network_id, lo, hi, limit, off))
                else:
                    sql = """
                        SELECT c.commit_id, c.commit_hash, c.author_date, c.subject,
                               p.person_id AS author_id, p.name AS author_name, p.email AS author_email,
                               (SELECT COUNT(*) FROM m_commit_file cf WHERE cf.commit_id = c.commit_id) AS files_count
                        FROM m_commit c
                        JOIN m_maintainer_person p ON c.author_id = p.person_id
                        WHERE c.commit_id > 0 AND c.network_id = %s AND c.commit_hash = %s
                        ORDER BY c.commit_id DESC LIMIT %s OFFSET %s;
                    """
                    cursor.execute(sql, (network_id, lo, limit, off))

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

                # If query is specified and we haven't reached the limit, search Git CLI for unparsed commits
                if len(commits) < limit:
                    known_hashes = {c["commit_hash"] for c in commits}
                    try:
                        out, _, rc = self.run_git(["rev-parse", "--verify", "--quiet", f"{q_clean}^{{commit}}"], timeout=5)
                        if rc == 0 and out.strip():
                            full_sha = out.strip()
                            if full_sha not in known_hashes:
                                g_out, _, g_rc = self.run_git(["log", "-1", "--format=%H%x00%an%x00%ae%x00%at%x00%s", full_sha], timeout=5)
                                if g_rc == 0 and g_out.strip():
                                    parts = g_out.strip().split("\x00")
                                    if len(parts) >= 5:
                                        adate = int(parts[3]) if parts[3].isdigit() else 0
                                        commits.append({
                                            "commit_id": parts[0],
                                            "commit_hash": parts[0],
                                            "author_date": adate,
                                            "subject": parts[4],
                                            "author": {"person_id": 0, "name": parts[1], "email": parts[2]},
                                            "author_name": parts[1],
                                            "author_email": parts[2],
                                            "files_count": 0,
                                        })
                    except Exception as exc:
                        logger.debug("Git search fallback for hash %s failed: %s", q_clean, exc)

            elif q_clean:
                # Case 2: Text search by commit subject or author name
                clean_q = sanitize_like_query(q_clean)
                sql = """
                    SELECT c.commit_id, c.commit_hash, c.author_date, c.subject,
                           p.person_id AS author_id, p.name AS author_name, p.email AS author_email,
                           (SELECT COUNT(*) FROM m_commit_file cf WHERE cf.commit_id = c.commit_id) AS files_count
                    FROM m_commit c
                    JOIN m_maintainer_person p ON c.author_id = p.person_id
                    WHERE c.commit_id > 0 AND c.network_id = %s
                      AND (c.subject LIKE %s OR p.name LIKE %s)
                    ORDER BY c.commit_id DESC LIMIT %s OFFSET %s;
                """
                cursor.execute(sql, (network_id, f"%{clean_q}%", f"%{clean_q}%", limit, off))
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
            else:
                # Case 3: Empty query -> latest indexed commits (primary key walk, excluding future commits)
                sql = """
                    SELECT c.commit_id, c.commit_hash, c.author_date, c.subject,
                           p.person_id AS author_id, p.name AS author_name, p.email AS author_email,
                           (SELECT COUNT(*) FROM m_commit_file cf WHERE cf.commit_id = c.commit_id) AS files_count
                    FROM m_commit c
                    JOIN m_maintainer_person p ON c.author_id = p.person_id
                    WHERE c.commit_id > 0 AND c.network_id = %s AND c.author_date <= %s
                    ORDER BY c.commit_id DESC LIMIT %s OFFSET %s;
                """
                cursor.execute(sql, (network_id, now_ts, limit, off))
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
                "commits": commits,
            }

    def get_commit_detail(self, commit_id: int | str) -> dict[str, Any]:
        """Retrieve full commit metadata, modified files, and trailers."""
        with get_db_cursor() as cursor:
            if str(commit_id).isdigit():
                cursor.execute("SELECT * FROM m_commit WHERE commit_id = %s AND commit_id > 0 LIMIT 1;", (int(commit_id),))
                c_row = cursor.fetchone()
            else:
                # commit_hash is BINARY(20); anything that is not hex cannot name a commit, and
                # encoding it to latin-1 would raise on a non-latin-1 character
                cid_str = str(commit_id).strip()
                if not _HEX_REF.fullmatch(cid_str.lower()):
                    raise HTTPException(status_code=400, detail="A commit id or hash (4-40 hex digits) must be specified.")
                if len(cid_str) == 40:
                    cursor.execute("SELECT * FROM m_commit WHERE commit_hash = %s LIMIT 1;", (bytes.fromhex(cid_str),))
                    c_row = cursor.fetchone()
                else:
                    # Short prefix: a range over the binary hash (every digit counts); two matches are ambiguous
                    lo, hi = commit_prefix_range(cid_str)
                    cursor.execute(
                        f"SELECT * FROM m_commit WHERE commit_hash >= %s{' AND commit_hash < %s' if hi else ''} LIMIT 2;",
                        (lo, hi) if hi else (lo,),
                    )
                    rows = cursor.fetchall()
                    if len(rows) > 1:
                        raise HTTPException(status_code=400, detail=f"Commit prefix '{cid_str}' is ambiguous; give more digits.")
                    c_row = rows[0] if rows else None
            if not c_row:
                if not str(commit_id).isdigit():
                    return self._get_git_commit_detail(str(commit_id).strip())
                raise HTTPException(status_code=404, detail=f"Commit '{commit_id}' not found.")

            cid = c_row["commit_id"]

            # Parents from the ingested DAG (a merge has several; a commit without rows is a root, or
            # its parents were not ingested: _fill_missing_commit_files then asks Git if it must)
            cursor.execute(
                """
                SELECT c.commit_hash
                FROM m_commit_parent p
                JOIN m_commit c ON c.commit_id = p.parent_commit_id
                WHERE p.commit_id = %s
                ORDER BY p.parent_idx ASC;
                """,
                (cid,),
            )
            parents = [format_commit_hash(r["commit_hash"]) for r in cursor.fetchall()]

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

            # Linked mailing list emails from m_commit_email
            tier_names = {
                1: "Authoritative Link / Message-ID",
                2: "Exact Patch-ID",
                3: "Blob Hash Match",
                4: "Maintainer / Bot Reference",
                5: "Metadata Match",
                6: "Pull Request Match",
            }
            linked_emails: list[dict[str, Any]] = []
            try:
                cursor.execute(
                    """
                    SELECT message_id, thread_id, relation_type, match_tier,
                           patch_version, patch_idx, patch_total
                    FROM m_commit_email
                    WHERE commit_id = %s
                    ORDER BY relation_type ASC, patch_idx ASC;
                    """,
                    (cid,),
                )
                for erow in cursor.fetchall():
                    rel_type = erow["relation_type"]
                    tier = erow["match_tier"]
                    mid = safe_decode(erow["message_id"]).strip("<> ")
                    linked_emails.append({
                        "message_id": mid,
                        "thread_id": erow["thread_id"],
                        "relation_type": rel_type,
                        "match_tier": tier,
                        "match_tier_name": tier_names.get(tier, "Matched"),
                        "patch_version": erow["patch_version"],
                        "patch_idx": erow["patch_idx"],
                        "patch_total": erow["patch_total"],
                        "lore_url": f"{LORE_BASE_URL}{mid}/",
                    })
            except Exception as exc:
                logger.debug("Failed to query m_commit_email for commit %d: %s", cid, exc)

            # LKML thread URL on the self-hosted public-inbox mirror
            subj = safe_decode(c_row["subject"])
            lore_url = linked_emails[0]["lore_url"] if linked_emails else f"{LORE_BASE_URL}?q={urllib.parse.quote_plus(subj or '')}"

            detail = {
                "commit_id": cid,
                "commit_hash": format_commit_hash(c_row["commit_hash"]),
                "subject": subj,
                "message": safe_decode(c_row["message"]),
                "author": author_dict,
                "author_name": author_dict["name"],
                "author_email": author_dict["email"],
                "author_date": c_row["author_date"],
                "files": files,
                "files_source": "db" if files else "none",
                "files_truncated": False,
                "is_merge": len(parents) > 1,
                "trailers": trailers,
                "contributors": contributors,
                "lore_url": lore_url,
                "linked_emails": linked_emails,
            }

        # `git log -p` suppresses merge diffs, so the parser stages no m_commit_file rows for a
        # merge: its first-parent delta is resolved from Git on demand, which keeps "Modified
        # Files" in agreement with the "Show changes" panel (which diffs against the first parent).
        self._fill_missing_commit_files(detail, parents)
        return detail

    def _get_git_commit_detail(self, commit_ref: str) -> dict[str, Any]:
        """Fallback to inspect an unparsed commit directly from Git repository."""
        ref = commit_ref.strip()
        if not is_safe_rev(ref) and not _HEX_REF.fullmatch(ref.lower()):
            raise HTTPException(status_code=400, detail="Invalid commit reference.")
        try:
            out, _, rc = self.run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], timeout=10)
            if rc != 0 or not out.strip():
                raise HTTPException(status_code=404, detail=f"Commit '{commit_ref}' not found.")
            sha = out.strip()
            out, _, _ = self.run_git(["rev-list", "--parents", "-n1", sha], timeout=10)
            parents = out.split()[1:]

            # %an (author name) \x00 %ae (author email) \x00 %at (author timestamp) \x00 %s (subject) \x00 %B (body)
            out, _, rc = self.run_git(["log", "-1", "--format=%an%x00%ae%x00%at%x00%s%x00%B", sha], timeout=10)
            if rc != 0:
                raise HTTPException(status_code=404, detail=f"Commit '{commit_ref}' not found.")
            parts = out.split("\x00", 4)
            if len(parts) < 5:
                raise HTTPException(status_code=500, detail="Failed to parse commit details from Git.")
            author_name, author_email, author_date_str, subj, message = parts
            author_date = int(author_date_str) if author_date_str.strip().isdigit() else 0
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("Git inspection for commit %s failed: %s", ref, exc)
            raise HTTPException(status_code=500, detail="Failed to inspect commit in Git.") from exc

        # Parse contributor trailers from commit message
        trailers = []
        contributors = []
        seen_contribs = set()
        TRAILER_ROLES = {
            "signed-off-by": (4, "Signed-off-by"),
            "reviewed-by": (5, "Reviewed-by"),
            "acked-by": (6, "Acked-by"),
            "tested-by": (7, "Tested-by"),
            "reported-by": (8, "Reported-by"),
            "suggested-by": (9, "Suggested-by"),
            "co-developed-by": (3, "Co-developed-by"),
        }
        for line in message.splitlines():
            line = line.strip()
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            key_clean = key.strip().lower()
            val_clean = val.strip()
            if key_clean in TRAILER_ROLES and "<" in val_clean and ">" in val_clean:
                role_type, role_name = TRAILER_ROLES[key_clean]
                trailers.append({"role_type": role_type, "role_name": role_name, "raw_val": val_clean})
                c_name = val_clean.split("<")[0].strip()
                c_email = val_clean.split("<")[1].split(">")[0].strip()
                c_key = (c_name.lower(), c_email.lower())
                if c_key not in seen_contribs:
                    seen_contribs.add(c_key)
                    contributors.append({
                        "person_id": 0,
                        "name": c_name,
                        "email": c_email,
                        "role_type": role_type,
                        "role_name": role_name,
                        "roles": [role_name],
                    })

        detail = {
            "commit_id": sha,
            "commit_hash": sha,
            "subject": subj,
            "message": message,
            "author": {"person_id": 0, "name": author_name, "email": author_email},
            "author_name": author_name,
            "author_email": author_email,
            "author_date": author_date,
            "files": [],
            "files_source": "none",
            "files_truncated": False,
            "is_merge": len(parents) > 1,
            "trailers": trailers,
            "contributors": contributors,
            "lore_url": f"{LORE_BASE_URL}?q={urllib.parse.quote_plus(subj or '')}",
            "linked_emails": [],
        }
        self._fill_missing_commit_files(detail, parents)
        return detail

    def _fill_missing_commit_files(self, detail: dict[str, Any], parents: list[str]) -> None:
        """Resolve a commit's first-parent file list from Git when no rows were staged for it.
        `parents` are the commit's parents from m_commit_parent (Git is asked only when there are none)."""
        if detail["files"]:
            return
        sha = detail.get("commit_hash") or ""
        if not _HEX_REF.fullmatch(sha.lower()):
            return
        try:
            if not parents:
                out, _, rc = self.run_git(["rev-list", "--parents", "-n1", sha], timeout=10)
                if rc != 0:
                    return
                parents = out.split()[1:]
                detail["is_merge"] = len(parents) > 1
            base = parents[0] if parents else _EMPTY_TREE
            out, _, rc = self.run_git(
                ["-c", "core.quotePath=false", "diff", "--no-color", "--no-ext-diff",
                 "-M", "--name-status", base, sha],
                timeout=60,
            )
            if rc != 0:
                return
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("Resolving the file list of commit %s from Git failed: %s", sha, exc)
            return

        files: list[dict[str, Any]] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2 or not parts[0]:
                continue
            # Renames and copies carry a similarity score ("R100") and both paths; the new path wins
            files.append({"change_type": parts[0][0].upper(), "file": parts[-1].strip()})
            if len(files) >= MAX_COMMIT_FILE_ROWS:
                detail["files_truncated"] = True
                break
        if files:
            detail["files"] = files
            detail["files_source"] = "git"

    def resolve_commit_mail(self, commit_id_or_hash: str) -> dict[str, Any]:
        """On-demand resolution of a commit against the mailing list archive."""
        detail = self.get_commit_detail(commit_id_or_hash)
        if detail.get("linked_emails"):
            return {"resolved": True, "linked_emails": detail["linked_emails"], "cached": True}

        from parser.mailing_list.ml_client import MailingListClient
        from parser.mailing_list.ml_types import CommitFeatures, MLRelationType, MLMatchTier

        repo_dir = str(get_repo_dir())
        c_hash = detail["commit_hash"]
        raw_cid = detail.get("commit_id", 0)
        try:
            cid = int(raw_cid)
        except (ValueError, TypeError):
            cid = 0

        patch_id = None
        try:
            # Bounded like get_commit_diff(): a tree-import commit diffs to hundreds of MB. A truncated
            # patch would give another patch id, so none is computed for it.
            with subprocess.Popen(["git", "-C", repo_dir, "show", "--format=", c_hash],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as show:
                patch = show.stdout.read(MAX_COMMIT_DIFF_BYTES + 1)
                if len(patch) > MAX_COMMIT_DIFF_BYTES:
                    show.kill()
                    patch = b""
                show_rc = show.wait(timeout=30)
            if show_rc == 0 and patch:
                p_patchid = subprocess.run(["git", "patch-id", "--stable"], input=patch, capture_output=True, check=False, timeout=30)
                if p_patchid.returncode == 0 and p_patchid.stdout:
                    parts = p_patchid.stdout.decode("ascii", errors="replace").split()
                    if parts:
                        patch_id = parts[0]
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("patch-id of %s not computed: %s", c_hash, exc)

        blob_hashes: list[str] = []
        try:
            diff_res = subprocess.run(["git", "-C", repo_dir, "diff-tree", "-r", "--no-commit-id", c_hash],
                                      capture_output=True, text=True, check=False, timeout=30)
            if diff_res.returncode == 0:
                for line in diff_res.stdout.splitlines():
                    parts = line.split()
                    if len(parts) >= 5:
                        post_sha = parts[3]
                        if len(post_sha) == 40 and post_sha != "0" * 40:
                            blob_hashes.append(post_sha)
        except Exception:
            pass

        features = CommitFeatures(
            commit_id=cid,
            commit_hash=c_hash,
            subject=detail.get("subject", ""),
            author_name=detail.get("author_name", ""),
            author_email=detail.get("author_email", ""),
            author_date=int(detail.get("author_date", 0) or 0),
            commit_message=detail.get("message", ""),
            patch_id=patch_id,
            blob_hashes=blob_hashes[:10],
        )

        try:
            client = MailingListClient()
            results = client.resolve_commits([features])
            ctx = results.get(c_hash)
            if not ctx:
                return {"resolved": False, "linked_emails": [], "reason": "No matching mailing list email found"}

            linked: list[dict[str, Any]] = []
            if ctx.patch_email and ctx.patch_email.mids:
                mid = ctx.patch_email.mids[0].strip("<> ")
                linked.append({
                    "message_id": mid,
                    "thread_id": ctx.patch_email.tid,
                    "relation_type": int(MLRelationType.Patch),
                    "match_tier": int(ctx.matched_tier),
                    "match_tier_name": "Live Resolved",
                    "patch_version": int(ctx.patch_version),
                    "patch_idx": int(ctx.patch_idx),
                    "patch_total": int(ctx.patch_total),
                    "lore_url": f"{LORE_BASE_URL}{mid}/",
                })
            if ctx.cover_letter and ctx.cover_letter.mids:
                c_mid = ctx.cover_letter.mids[0].strip("<> ")
                linked.append({
                    "message_id": c_mid,
                    "thread_id": ctx.cover_letter.tid,
                    "relation_type": int(MLRelationType.CoverLetter),
                    "match_tier": int(ctx.matched_tier),
                    "match_tier_name": "Live Resolved",
                    "patch_version": int(ctx.patch_version),
                    "patch_idx": 0,
                    "patch_total": int(ctx.patch_total),
                    "lore_url": f"{LORE_BASE_URL}{c_mid}/",
                })
            if ctx.pull_request and ctx.pull_request.mids:
                pr_mid = ctx.pull_request.mids[0].strip("<> ")
                linked.append({
                    "message_id": pr_mid,
                    "thread_id": ctx.pull_request.tid,
                    "relation_type": int(MLRelationType.PullRequest),
                    "match_tier": int(MLMatchTier.PullRequest),
                    "match_tier_name": "Pull Request",
                    "patch_version": 1,
                    "patch_idx": 0,
                    "patch_total": 0,
                    "lore_url": f"{LORE_BASE_URL}{pr_mid}/",
                })
            return {"resolved": bool(linked), "linked_emails": linked, "cached": False}
        except Exception as exc:
            logger.warning("Live resolve error for commit %s: %s", c_hash, exc)
            return {"resolved": False, "linked_emails": [], "error": str(exc)}

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
            logger.warning("git diff for commit %s failed: %s", ref, e)
            raise HTTPException(status_code=500, detail="git diff failed") from e
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

    def get_commit_timeline(
        self,
        version_name: str,
        limit: int = 100,
        direction: str = "past",
        cursor_id: int | None = None,
    ) -> dict[str, Any]:
        """Retrieve structured commit timeline starting from the version tag anchor, supporting past and future exploration."""
        import datetime

        limit = max(1, min(int(limit or 100), 250))

        def _format_date(ts: int) -> str:
            try:
                return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
            except Exception:
                return ""

        now_ts = int(time.time())

        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                vid, vname = get_version_info(cursor, "v3.0")
            # Commit ids are global: every query below stays inside the repository's Git network
            network_id = get_active_network_id(cursor)
            if network_id is None:
                network_id = -1

            anchor_row = None
            if vid and vid > 0:
                cursor.execute(
                    """
                    SELECT c.commit_id
                    FROM m_commit c
                    WHERE c.commit_id = %s AND c.network_id = %s AND c.author_date <= %s
                    LIMIT 1;
                    """,
                    (vid, network_id, now_ts),
                )
                anchor_row = cursor.fetchone()

            if not anchor_row:
                # Check if version_name resolves directly in Git as an unparsed commit or branch
                if is_safe_rev(version_name) or _HEX_REF.fullmatch(version_name.lower()):
                    try:
                        out, _, rc = self.run_git(["rev-parse", "--verify", "--quiet", f"{version_name}^{{commit}}"], timeout=5)
                        if rc == 0 and out.strip():
                            git_sha = out.strip()
                            g_out, _, g_rc = self.run_git(
                                ["log", f"-n{limit}", "--format=%H%x00%an%x00%ae%x00%at%x00%s", git_sha],
                                timeout=10,
                            )
                            if g_rc == 0 and g_out.strip():
                                git_commits = []
                                for line in g_out.strip().splitlines():
                                    parts = line.split("\x00")
                                    if len(parts) >= 5:
                                        adate = int(parts[3]) if parts[3].isdigit() else 0
                                        if adate > now_ts:
                                            continue
                                        name = parts[1] or "Unknown"
                                        email = parts[2] or ""
                                        git_commits.append({
                                            "commit_id": parts[0],
                                            "commit_hash": parts[0],
                                            "author_date": adate,
                                            "author_date_iso": _format_date(adate),
                                            "subject": parts[4],
                                            "author": {"person_id": 0, "name": name, "email": email},
                                            "author_name": name,
                                            "author_email": email,
                                            "files_count": 0,
                                        })
                                if git_commits:
                                    return {
                                        "version": version_name,
                                        "direction": (direction or "past").lower().strip(),
                                        "has_past": len(git_commits) >= limit,
                                        "has_future": False,
                                        "next_past_cursor": None,
                                        "next_future_cursor": None,
                                        "timeline": git_commits,
                                    }
                    except Exception as e:
                        logger.debug("Failed to resolve unparsed git timeline for %s: %s", version_name, e)

                # Find closest commit or latest tag commit (strictly not beyond current date)
                cursor.execute(
                    """
                    SELECT c.commit_id
                    FROM m_commit c
                    WHERE c.commit_id > 0 AND c.network_id = %s AND c.author_date <= %s
                    ORDER BY c.commit_id DESC
                    LIMIT 1;
                    """,
                    (network_id, now_ts),
                )
                anchor_row = cursor.fetchone()

            if not anchor_row:
                return {
                    "version": version_name,
                    "direction": direction,
                    "timeline": [],
                }

            anchor_id = anchor_row["commit_id"]

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
                    WHERE c.commit_id >= %s AND c.network_id = %s AND c.author_date <= %s
                    ORDER BY c.commit_id ASC
                    LIMIT %s;
                    """,
                    (start_id, network_id, now_ts, limit),
                )
                raw_rows = cursor.fetchall()
            elif dir_mode == "around":
                future_limit = max(5, limit // 4)
                past_limit = max(1, limit - future_limit)
                cursor.execute(
                    """
                    SELECT c.commit_id, c.commit_hash, c.author_date, c.subject,
                           p.person_id AS author_id, p.name AS author_name, p.email AS author_email,
                           (SELECT COUNT(*) FROM m_commit_file cf WHERE cf.commit_id = c.commit_id) AS files_count
                    FROM m_commit c
                    JOIN m_maintainer_person p ON c.author_id = p.person_id
                    WHERE c.commit_id > %s AND c.network_id = %s AND c.author_date <= %s
                    ORDER BY c.commit_id ASC
                    LIMIT %s;
                    """,
                    (anchor_id, network_id, now_ts, future_limit),
                )
                future_rows = list(reversed(cursor.fetchall()))
                cursor.execute(
                    """
                    SELECT c.commit_id, c.commit_hash, c.author_date, c.subject,
                           p.person_id AS author_id, p.name AS author_name, p.email AS author_email,
                           (SELECT COUNT(*) FROM m_commit_file cf WHERE cf.commit_id = c.commit_id) AS files_count
                    FROM m_commit c
                    JOIN m_maintainer_person p ON c.author_id = p.person_id
                    WHERE c.commit_id <= %s AND c.network_id = %s AND c.author_date <= %s
                    ORDER BY c.commit_id DESC
                    LIMIT %s;
                    """,
                    (anchor_id, network_id, now_ts, past_limit),
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
                    WHERE c.commit_id <= %s AND c.network_id = %s AND c.author_date <= %s
                    ORDER BY c.commit_id DESC
                    LIMIT %s;
                    """,
                    (start_id, network_id, now_ts, limit),
                )
                raw_rows = cursor.fetchall()

            commits = []
            for r in raw_rows:
                name = safe_decode(r["author_name"]) or "Unknown"
                email = safe_decode(r["author_email"]) or ""
                commits.append({
                    "commit_id": r["commit_id"],
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
                })

            min_id = min((c["commit_id"] for c in commits), default=anchor_id)
            max_id = max((c["commit_id"] for c in commits), default=anchor_id)

            cursor.execute("SELECT 1 FROM m_commit WHERE commit_id < %s AND commit_id > 0 AND network_id = %s LIMIT 1;", (min_id, network_id))
            has_past = cursor.fetchone() is not None
            cursor.execute("SELECT 1 FROM m_commit WHERE commit_id > %s AND network_id = %s AND author_date <= %s LIMIT 1;", (max_id, network_id, now_ts))
            has_future = cursor.fetchone() is not None

            return {
                "version": vname,
                "direction": dir_mode,
                "has_past": has_past,
                "has_future": has_future,
                "next_past_cursor": max(1, min_id - 1),
                "next_future_cursor": max_id + 1,
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

