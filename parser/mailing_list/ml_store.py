"""parser/mailing_list/ml_store.py - Direct read-only interface to public-inbox v2 / extindex.

Direct read-only interface to public-inbox SQLite databases (over.sqlite3, msgmap.sqlite3),
sharded Xapian search indices, and all.git blob storage.
"""
from __future__ import annotations

import logging
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time
from typing import Any
import urllib.parse
import zlib

from parser.mailing_list.ml_types import SmsgRecord

try:
    import xapian
    HAS_XAPIAN = True
except ImportError:
    xapian = None  # type: ignore[assignment]
    HAS_XAPIAN = False

logger = logging.getLogger(__name__)


class PublicInboxLocalStore:
    """Direct read-only interface to public-inbox SQLite, Xapian shards, and all.git."""

    BOOLEAN_PREFIXES = {
        "m": "Q",
        "patchid": "XPATCHID",
        "dfpost": "XDFPOST",
        "dfpre": "XDFPRE",
    }
    PROB_PREFIXES = {
        "s": "S",
        "f": "A",
        "dfn": "XDFN",
        "nq": "XNQ",
    }

    def __init__(self, inbox_root: str | Path):
        self.root = Path(inbox_root)
        if not self.root.exists():
            raise FileNotFoundError(f"Public-inbox root directory does not exist: {self.root}")

        # Check for unified extindex directory (e.g. /srv/lore/all) first
        all_dir = self.root / "all"
        if all_dir.is_dir() and (any(all_dir.glob("ei*")) or any(all_dir.glob("xap*")) or (all_dir / "over.sqlite3").exists()):
            self.root = all_dir

        idx_candidates = sorted(
            list(self.root.glob("xap*")) + list(self.root.glob("ei*"))
        )
        if not idx_candidates:
            # Check if root itself is the index directory (e.g. contains over.sqlite3 directly)
            if (self.root / "over.sqlite3").exists():
                self.idx_dir = self.root
            else:
                raise FileNotFoundError(f"No xap* or ei* index directory in {self.root}")
        else:
            self.idx_dir = idx_candidates[-1]

        # 1. Store database paths and locks for thread-safe access
        self._over_path = self.idx_dir / "over.sqlite3"
        if not self._over_path.exists():
            raise FileNotFoundError(f"Missing over.sqlite3 at {self._over_path}")

        msgmap_file = self.root / "msgmap.sqlite3"
        if not msgmap_file.exists():
            msgmap_file = self.idx_dir / "msgmap.sqlite3"
        self._msgmap_path = msgmap_file if msgmap_file.exists() else None

        self._local = threading.local()
        self._over_db_override: Any = None
        self._msgmap_db_override: Any = None
        self._cat_lock = threading.Lock()
        self._qp_lock = threading.Lock()

        # 2. Open all Xapian shards into a unified Database handle
        self.xap_db: Any = None
        self.qp: Any = None
        self.shard_count = 0

        if HAS_XAPIAN and xapian is not None:
            shard_dirs = sorted(
                [p for p in self.idx_dir.iterdir() if p.is_dir() and p.name.isdigit()],
                key=lambda p: int(p.name),
            )
            self.shard_count = len(shard_dirs)
            if self.shard_count > 0:
                self.xap_db = xapian.Database()
                for sd in shard_dirs:
                    self.xap_db.add_database(xapian.Database(str(sd)))

                self.qp = xapian.QueryParser()
                self.qp.set_database(self.xap_db)
                self.qp.set_stemmer(xapian.Stem("english"))
                self.qp.set_stemming_strategy(xapian.QueryParser.STEM_SOME)
                self.qp.set_default_op(xapian.Query.OP_AND)
                for u_pfx, x_pfx in self.BOOLEAN_PREFIXES.items():
                    self.qp.add_boolean_prefix(u_pfx, x_pfx)
                for u_pfx, x_pfx in self.PROB_PREFIXES.items():
                    self.qp.add_prefix(u_pfx, x_pfx)
        else:
            logger.info("python-xapian bindings not available; Xapian shard querying disabled.")

        # 3. Locate git repository (ALL.git / all.git / component inboxes) for raw email blobs
        self._all_git = self._find_all_git()
        self._cat_proc: subprocess.Popen[bytes] | None = None
        if self._all_git:
            self._ensure_cat_proc()

    def _find_all_git(self) -> Path | None:
        """Locate the git repository providing raw blob storage."""
        candidates = [
            self.root / "ALL.git",
            self.root / "all.git",
            self.idx_dir / "ALL.git",
            self.idx_dir / "all.git",
            self.root / "all" / "ALL.git",
            self.root / "all" / "all.git",
            self.root.parent / "ALL.git" if self.root.parent else None,
            self.root.parent / "all.git" if self.root.parent else None,
            self.root.parent / "all" / "ALL.git" if self.root.parent else None,
            self.root / "git" / "0.git",
            self.root / "0.git",
        ]
        for cand in candidates:
            if cand and cand.exists():
                return cand

        # Fallback globs
        for pattern in ("*[aA][lL][lL]*.git", "*.git", "git/*.git"):
            for cand in self.root.glob(pattern):
                if cand.is_dir():
                    return cand
            for cand in self.idx_dir.glob(pattern):
                if cand.is_dir():
                    return cand
        return None

    def _ensure_cat_proc(self) -> subprocess.Popen[bytes] | None:
        """Ensure git cat-file --batch process is running and healthy."""
        if not self._all_git:
            self._all_git = self._find_all_git()
        if not self._all_git or not self._all_git.exists():
            return None

        if self._cat_proc is not None and self._cat_proc.poll() is None:
            return self._cat_proc

        try:
            self._cat_proc = subprocess.Popen(
                ["git", "--git-dir", str(self._all_git), "cat-file", "--batch"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                bufsize=0,
            )
            logger.info("Started git cat-file --batch against %s", self._all_git)
            return self._cat_proc
        except Exception as exc:
            logger.warning("Could not start git cat-file process against %s: %s", self._all_git, exc)
            self._cat_proc = None
            return None

    @staticmethod
    def _open_sqlite(path: Path) -> sqlite3.Connection:
        """Open a read-only SQLite handle configured for resilient concurrent access."""
        conn = sqlite3.connect(
            f"file:{path}?mode=ro&immutable=1",
            uri=True,
            check_same_thread=False,
            timeout=60.0,
            isolation_level=None,
        )
        try:
            conn.execute("PRAGMA busy_timeout = 60000;")
            conn.execute("PRAGMA query_only = ON;")
        except Exception:
            pass
        return conn

    @property
    def over_db(self) -> sqlite3.Connection:
        if self._over_db_override is not None:
            return self._over_db_override
        db = getattr(self._local, "over_db", None)
        if db is None:
            db = self._open_sqlite(self._over_path)
            self._local.over_db = db
        return db

    @over_db.setter
    def over_db(self, val: Any) -> None:
        self._over_db_override = val

    @property
    def msgmap_db(self) -> sqlite3.Connection | None:
        if self._msgmap_db_override is not None:
            return self._msgmap_db_override
        if not self._msgmap_path:
            return None
        db = getattr(self._local, "msgmap_db", None)
        if db is None:
            db = self._open_sqlite(self._msgmap_path)
            self._local.msgmap_db = db
        return db

    @msgmap_db.setter
    def msgmap_db(self, val: Any) -> None:
        self._msgmap_db_override = val

    def _execute_query(
        self,
        db: sqlite3.Connection | None,
        sql: str,
        params: tuple[Any, ...] = (),
        retries: int = 5,
        base_delay: float = 0.2,
    ) -> sqlite3.Cursor:
        """Execute a query with exponential backoff on concurrent SQLite locks."""
        if db is None:
            raise sqlite3.OperationalError("Database connection is None")
        for attempt in range(retries):
            try:
                return db.execute(sql, params)
            except sqlite3.OperationalError as exc:
                err_msg = str(exc).lower()
                if ("locked" in err_msg or "busy" in err_msg) and attempt < retries - 1:
                    sleep_time = base_delay * (2 ** attempt)
                    logger.warning(
                        "SQLite lock contention (attempt %d/%d), retrying in %.2fs: %s",
                        attempt + 1,
                        retries,
                        sleep_time,
                        exc,
                    )
                    time.sleep(sleep_time)
                else:
                    raise
        return db.execute(sql, params)

    def close(self) -> None:
        """Safely close open databases and processes."""
        db = getattr(self._local, "over_db", None)
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
            self._local.over_db = None

        msg_db = getattr(self._local, "msgmap_db", None)
        if msg_db is not None:
            try:
                msg_db.close()
            except Exception:
                pass
            self._local.msgmap_db = None

        with self._cat_lock:
            if self._cat_proc:
                try:
                    if self._cat_proc.stdin:
                        self._cat_proc.stdin.close()
                    self._cat_proc.terminate()
                    self._cat_proc.wait(timeout=1.0)
                except Exception:
                    pass
                self._cat_proc = None

    @staticmethod
    def _unpack_row(row: tuple[int, int, int, int, int, bytes]) -> SmsgRecord:
        num, tid, sid, ts, ds, ddd = row
        raw = zlib.decompress(ddd).decode("utf-8", errors="replace") if ddd else ""
        lines = raw.split("\n")

        subj = lines[0] if len(lines) > 0 else ""
        auth = lines[1] if len(lines) > 1 else ""
        refs_str = lines[2] if len(lines) > 2 else ""
        references = [r.strip("<> \t\r\n") for r in refs_str.split() if r.strip("<> \t\r\n")]

        git_blob: str | None = None
        mids: list[str] = []
        msg_bytes = 0
        msg_lines = 0

        # Public-inbox standard ddd format has >= 7 lines:
        # [0]=Subject, [1]=From, [2]=References, [3]=To, [4]=Cc, [5]=Blob OID, [6]=Message-ID, [7]=Bytes, [8]=Lines
        if len(lines) >= 7:
            blob_cand = lines[5].strip()
            if len(blob_cand) == 40 and not any(c in blob_cand for c in " \t<>@"):
                git_blob = blob_cand
            mids = [m.strip("<> \t\r\n") for m in lines[6].split() if m.strip("<> \t\r\n")]
            if len(lines) > 7 and lines[7].strip().isdigit():
                msg_bytes = int(lines[7].strip())
            if len(lines) > 8 and lines[8].strip().isdigit():
                msg_lines = int(lines[8].strip())
        else:
            # Fallback for synthetic / 5-line test payloads:
            # [0]=Subject, [1]=From, [2]=References, [3]=Message-IDs, [4]=KV tokens
            mids = [m.strip("<> \t\r\n") for m in (lines[3].split() if len(lines) > 3 else []) if m.strip("<> \t\r\n")]
            kv = lines[4] if len(lines) > 4 else ""
            git_blob = next(
                (tok[1:].strip() for tok in kv.split(" ") if tok.startswith(":") and len(tok.strip()) > 7),
                None,
            )

        return SmsgRecord(
            num=num,
            tid=tid,
            sid=sid,
            ts=ts,
            ds=ds,
            subject=subj,
            author=auth,
            references=references,
            mids=mids,
            git_blob=git_blob,
            bytes=msg_bytes,
            lines=msg_lines,
        )

    def get_by_num(self, num: int) -> SmsgRecord | None:
        """Fetch article by sequential article number (> 0)."""
        if num <= 0:
            return None
        cursor = self._execute_query(
            self.over_db,
            "SELECT num, tid, sid, ts, ds, ddd FROM over WHERE num = ? AND num > 0 LIMIT 1",
            (num,),
        )
        row = cursor.fetchone()
        return self._unpack_row(row) if row else None

    def get_by_mid(self, mid: str) -> SmsgRecord | None:
        """Fetch article by RFC822 Message-ID (mid)."""
        raw_mid = str(mid).strip("<> \t\r\n")
        clean_mid = urllib.parse.unquote(raw_mid).strip("<> \t\r\n")
        if not clean_mid and not raw_mid:
            return None

        candidates: list[str] = []
        for cand in (clean_mid, raw_mid, f"<{clean_mid}>", f"<{raw_mid}>"):
            if cand and cand not in candidates:
                candidates.append(cand)

        placeholders = ",".join("?" for _ in candidates)

        # Check msgmap.sqlite3 if present
        if self.msgmap_db is not None:
            cursor = self._execute_query(
                self.msgmap_db,
                f"SELECT num FROM msg_map WHERE mid IN ({placeholders}) LIMIT 1",
                tuple(candidates),
            )
            row = cursor.fetchone()
            if row and row[0] > 0:
                return self.get_by_num(row[0])

        # Fallback for extindex schema (msgid + id2num inside over.sqlite3)
        try:
            cursor = self._execute_query(
                self.over_db,
                f"""
                SELECT i2n.num FROM msgid m
                JOIN id2num i2n ON m.id = i2n.id
                WHERE m.mid IN ({placeholders}) AND i2n.num > 0 LIMIT 1
                """,
                tuple(candidates),
            )
            row = cursor.fetchone()
            if row and row[0] > 0:
                return self.get_by_num(row[0])
        except sqlite3.OperationalError:
            pass
        return None

    def get_thread_by_tid(self, tid: int) -> list[SmsgRecord]:
        """Fetch all non-ghost messages in a thread ordered chronologically."""
        if tid <= 0:
            return []
        cursor = self._execute_query(
            self.over_db,
            """
            SELECT num, tid, sid, ts, ds, ddd
            FROM over
            WHERE tid = ? AND num > 0
            ORDER BY ds ASC, num ASC
            """,
            (tid,),
        )
        rows = cursor.fetchall()
        return [self._unpack_row(r) for r in rows]

    def search_over_subjects(self, pattern: str, limit: int = 20) -> list[SmsgRecord]:
        """Query over table by subject substring pattern (fallback when Xapian unavailable)."""
        cursor = self._execute_query(
            self.over_db,
            """
            SELECT num, tid, sid, ts, ds, ddd
            FROM over
            WHERE num > 0
            ORDER BY ds DESC
            LIMIT 500
            """,
        )
        matches: list[SmsgRecord] = []
        pattern_lower = pattern.lower()
        for r in cursor.fetchall():
            rec = self._unpack_row(r)
            if pattern_lower in rec.subject.lower():
                matches.append(rec)
                if len(matches) >= limit:
                    break
        return matches

    def execute_xapian(self, query: Any, limit: int = 50) -> list[SmsgRecord]:
        """Execute a Xapian query with automatic reopen() on concurrent writer updates."""
        if self.xap_db is None or not HAS_XAPIAN or xapian is None:
            return []

        for attempt in range(2):
            try:
                enquire = xapian.Enquire(self.xap_db)
                enquire.set_query(query)
                enquire.set_weighting_scheme(xapian.BoolWeight())
                enquire.set_sort_by_value(0, True)
                mset = enquire.get_mset(0, limit)

                results: list[SmsgRecord] = []
                for match in mset:
                    for term_item in match.document.termlist():
                        t = term_item.term.decode("utf-8", errors="replace")
                        if t.startswith("Q"):
                            rec = self.get_by_mid(t[1:])
                            if rec:
                                results.append(rec)
                                break
                return results
            except getattr(xapian, "DatabaseModifiedError", Exception):
                if attempt == 0:
                    self.xap_db.reopen()
                else:
                    raise
        return []

    def get_raw_email_headers(self, blob_oid: str) -> dict[str, str]:
        """Read RFC822 email headers from all.git blob OID."""
        with self._cat_lock:
            proc = self._ensure_cat_proc()
            if not proc or not proc.stdin or not proc.stdout:
                return {}
            try:
                proc.stdin.write(f"{blob_oid}\n".encode("ascii"))
                proc.stdin.flush()
                header = proc.stdout.readline().decode("ascii").strip()
                if header.endswith("missing"):
                    return {}
                parts = header.split()
                if len(parts) < 3:
                    return {}
                size = int(parts[2])
                payload = proc.stdout.read(size)
                proc.stdout.read(1)  # trailing LF

                headers: dict[str, str] = {}
                for line in payload.decode("utf-8", errors="replace").splitlines():
                    if not line.strip():
                        break
                    if ":" in line and not line.startswith((" ", "\t")):
                        k, v = line.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
                return headers
            except Exception as exc:
                logger.debug("Failed to read raw email blob %s: %s", blob_oid, exc)
                try:
                    proc.terminate()
                except Exception:
                    pass
                self._cat_proc = None
                return {}

    # Aliases and helper lookups
    lookup_by_mid = get_by_mid
    lookup_thread = get_thread_by_tid

    def lookup_by_blob(self, blob_sha: str) -> list[SmsgRecord]:
        """Fetch all messages matching a git blob SHA in their overview data."""
        clean_sha = blob_sha.strip().lower()
        if len(clean_sha) == 40:
            try:
                raw_bin = bytes.fromhex(clean_sha)
                cursor = self._execute_query(
                    self.over_db,
                    "SELECT docid FROM xref3 WHERE oidbin = ? LIMIT 10",
                    (raw_bin,),
                )
                docids = [row[0] for row in cursor.fetchall() if row[0] > 0]
                if docids:
                    matches: list[SmsgRecord] = []
                    for docid in docids:
                        rec = self.get_by_num(docid)
                        if rec:
                            matches.append(rec)
                    return matches
            except sqlite3.OperationalError:
                pass
            except Exception as exc:
                logger.debug("xref3 lookup error for blob %s: %s", clean_sha, exc)

        cursor = self._execute_query(
            self.over_db,
            "SELECT num, tid, sid, ts, ds, ddd FROM over WHERE num > 0 ORDER BY ds DESC LIMIT 1000",
        )
        matches: list[SmsgRecord] = []
        for r in cursor.fetchall():
            rec = self._unpack_row(r)
            if rec.git_blob and clean_sha in rec.git_blob.lower():
                matches.append(rec)
        return matches

    def get_raw_email(self, blob_oid: str) -> str:
        """Read full RFC822 raw email content from all.git blob OID."""
        clean_oid = blob_oid.strip()
        if not clean_oid or len(clean_oid) != 40:
            return ""
        with self._cat_lock:
            proc = self._ensure_cat_proc()
            if not proc or not proc.stdin or not proc.stdout:
                return ""
            try:
                proc.stdin.write(f"{clean_oid}\n".encode("ascii"))
                proc.stdin.flush()
                header = proc.stdout.readline().decode("ascii").strip()
                if header.endswith("missing"):
                    return ""
                parts = header.split()
                if len(parts) < 3:
                    return ""
                size = int(parts[2])
                payload = proc.stdout.read(size)
                proc.stdout.read(1)  # trailing LF
                return payload.decode("utf-8", errors="replace")
            except Exception as exc:
                logger.debug("Failed to read raw email blob %s: %s", clean_oid, exc)
                try:
                    proc.terminate()
                except Exception:
                    pass
                self._cat_proc = None
                return ""

    def get_email_detail(self, mid_or_num: str | int) -> dict[str, Any] | None:
        """Fetch full email record with parsed headers, body, patch diffs, and raw payload."""
        rec: SmsgRecord | None = None
        raw_str = str(mid_or_num).strip("<> \t\r\n")
        clean_target = urllib.parse.unquote(raw_str).strip("<> \t\r\n")
        if isinstance(mid_or_num, int) or clean_target.isdigit():
            rec = self.get_by_num(int(clean_target))
        else:
            rec = self.get_by_mid(clean_target)

        if not rec:
            return None

        from datetime import datetime, timezone
        from parser.mailing_list.ml_patch_parser import parse_patch_from_body
        from parser.mailing_list.ml_resolver import extract_patch_metadata
        import email
        from email import policy

        raw_content = ""
        blob_sha = rec.git_blob
        if not blob_sha:
            try:
                cur = self._execute_query(
                    self.over_db,
                    "SELECT hex(oidbin) FROM xref3 WHERE docid = ? LIMIT 1",
                    (rec.num,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    blob_sha = str(row[0]).lower()
            except Exception:
                pass

        if blob_sha:
            raw_content = self.get_raw_email(blob_sha)

        headers: dict[str, str] = {}
        body = ""
        if raw_content:
            try:
                msg = email.message_from_string(raw_content, policy=policy.compat32)
                for k, v in msg.items():
                    headers[k.lower()] = str(v)
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            payload = part.get_payload(decode=True)
                            if payload:
                                charset = part.get_content_charset() or "utf-8"
                                body = payload.decode(charset, errors="replace")
                                break
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        charset = msg.get_content_charset() or "utf-8"
                        body = payload.decode(charset, errors="replace")
                    else:
                        body = str(msg.get_payload() or "")
            except Exception as exc:
                logger.debug("Error parsing email MIME structure: %s", exc)

        if not body and raw_content:
            parts = raw_content.split("\n\n", 1)
            body = parts[1] if len(parts) > 1 else raw_content

        patch_info = parse_patch_from_body(body)

        dt_iso = ""
        if rec.ds or rec.ts:
            try:
                dt_iso = datetime.fromtimestamp(rec.ds or rec.ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            except Exception:
                pass

        patch_v, patch_idx, patch_total = extract_patch_metadata(rec.subject)

        primary_mid = rec.mids[0] if rec.mids else ""
        return {
            "num": rec.num,
            "tid": rec.tid,
            "sid": rec.sid,
            "subject": rec.subject,
            "author": rec.author,
            "timestamp": rec.ds or rec.ts,
            "date_iso": dt_iso,
            "message_id": primary_mid,
            "mids": rec.mids,
            "references": rec.references,
            "git_blob": rec.git_blob,
            "bytes": rec.bytes,
            "lines": rec.lines,
            "headers": headers,
            "raw": raw_content,
            "body": patch_info.get("body", body),
            "is_patch": patch_info.get("is_patch", False),
            "diff": patch_info.get("diff"),
            "patch_version": patch_v,
            "patch_idx": patch_idx,
            "patch_total": patch_total,
        }

    def get_thread_messages(self, tid: int) -> dict[str, Any]:
        """Fetch all messages in a thread with summary metadata and reply structure."""
        records = self.get_thread_by_tid(tid)
        if not records:
            return {"tid": tid, "subject": "", "total": 0, "participants": [], "messages": []}

        from datetime import datetime, timezone
        from parser.mailing_list.ml_resolver import extract_patch_metadata

        messages: list[dict[str, Any]] = []
        participants: set[str] = set()
        root_subject = records[0].subject if records else ""

        for r in records:
            if r.author:
                participants.add(r.author)
            dt_iso = ""
            if r.ds or r.ts:
                try:
                    dt_iso = datetime.fromtimestamp(r.ds or r.ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass

            patch_v, patch_idx, patch_total = extract_patch_metadata(r.subject)
            is_patch = bool(re.search(r"\[[^\]]*\bPATCH\b", r.subject, re.IGNORECASE))
            is_cover = bool(re.search(r"\[[^\]]*\b0+/\d+[^\]]*\]", r.subject))
            is_pull = bool(re.search(r"\[[^\]]*\bGIT PULL\b", r.subject, re.IGNORECASE))

            mid = r.mids[0] if r.mids else ""
            messages.append({
                "num": r.num,
                "tid": r.tid,
                "sid": r.sid,
                "subject": r.subject,
                "author": r.author,
                "timestamp": r.ds or r.ts,
                "date_iso": dt_iso,
                "message_id": mid,
                "mids": r.mids,
                "references": r.references,
                "git_blob": r.git_blob,
                "bytes": r.bytes,
                "lines": r.lines,
                "is_patch": is_patch,
                "is_cover": is_cover,
                "is_pull": is_pull,
                "patch_version": patch_v,
                "patch_idx": patch_idx,
                "patch_total": patch_total,
            })

        return {
            "tid": tid,
            "subject": root_subject,
            "total": len(messages),
            "participants": sorted(list(participants)),
            "messages": messages,
        }

    def search_emails(
        self,
        query: str = "",
        limit: int = 50,
        offset: int = 0,
        filter_type: str = "all",
    ) -> dict[str, Any]:
        """Search mailing list messages with filter support and pagination."""
        from datetime import datetime, timezone
        from parser.mailing_list.ml_resolver import extract_patch_metadata

        clean_q = (query or "").strip()
        matched_records: list[SmsgRecord] = []

        # 1. Try Xapian search if available and query is present
        if clean_q and self.xap_db is not None and HAS_XAPIAN and self.qp is not None and xapian is not None:
            try:
                x_query = self.qp.parse_query(clean_q)
                matched_records = self.execute_xapian(x_query, limit=max(200, limit + offset))
            except Exception as exc:
                logger.debug("Xapian query error: %s, falling back to SQLite search", exc)

        # 2. Fallback to over.sqlite3 substring search
        if not matched_records:
            if clean_q:
                matched_records = self.search_over_subjects(clean_q, limit=max(200, limit + offset))
            else:
                # Empty query: fetch recent messages ordered chronologically
                try:
                    cursor = self._execute_query(
                        self.over_db,
                        """
                        SELECT num, tid, sid, ts, ds, ddd
                        FROM over
                        WHERE num > 0
                        ORDER BY ds DESC
                        LIMIT ?
                        """,
                        (max(200, limit + offset),),
                    )
                    matched_records = [self._unpack_row(r) for r in cursor.fetchall()]
                except Exception as exc:
                    logger.warning("Empty search query failed: %s", exc)
                    matched_records = []

        # 3. Apply filter_type: all, patch, cover, pull
        filtered: list[SmsgRecord] = []
        for r in matched_records:
            subj_lower = r.subject.lower()
            if filter_type == "patch":
                if not re.search(r"\[[^\]]*\bpatch\b", subj_lower):
                    continue
            elif filter_type == "cover":
                if not re.search(r"\[[^\]]*\b0+/\d+[^\]]*\]", subj_lower):
                    continue
            elif filter_type == "pull":
                if not re.search(r"\[[^\]]*\bgit pull\b", subj_lower):
                    continue
            filtered.append(r)

        total = len(filtered)
        page = filtered[offset : offset + limit]

        results: list[dict[str, Any]] = []
        for r in page:
            dt_iso = ""
            if r.ds or r.ts:
                try:
                    dt_iso = datetime.fromtimestamp(r.ds or r.ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass

            patch_v, patch_idx, patch_total = extract_patch_metadata(r.subject)
            is_patch = bool(re.search(r"\[[^\]]*\bPATCH\b", r.subject, re.IGNORECASE))
            is_cover = bool(re.search(r"\[[^\]]*\b0+/\d+[^\]]*\]", r.subject))
            is_pull = bool(re.search(r"\[[^\]]*\bGIT PULL\b", r.subject, re.IGNORECASE))

            results.append({
                "num": r.num,
                "tid": r.tid,
                "sid": r.sid,
                "subject": r.subject,
                "author": r.author,
                "timestamp": r.ds or r.ts,
                "date_iso": dt_iso,
                "message_id": r.mids[0] if r.mids else "",
                "mids": r.mids,
                "is_patch": is_patch,
                "is_cover": is_cover,
                "is_pull": is_pull,
                "patch_version": patch_v,
                "patch_idx": patch_idx,
                "patch_total": patch_total,
            })

        return {
            "query": clean_q,
            "filter_type": filter_type,
            "total": total,
            "limit": limit,
            "offset": offset,
            "results": results,
        }

    def get_cover_letter(self, tid: int) -> SmsgRecord | None:
        """Find the series cover letter [0/N] in a thread."""
        thread = self.get_thread_by_tid(tid)
        for rec in thread:
            if re.search(r"\[[^\]]*\b0+/\d+[^\]]*\]", rec.subject):
                return rec
        return None


