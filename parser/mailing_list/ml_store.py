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
from typing import Any
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

        # 1. Open over.sqlite3 and msgmap.sqlite3 in read-only URI mode (?mode=ro)
        over_path = self.idx_dir / "over.sqlite3"
        if not over_path.exists():
            raise FileNotFoundError(f"Missing over.sqlite3 at {over_path}")

        over_uri = f"file:{over_path}?mode=ro"
        self.over_db = sqlite3.connect(over_uri, uri=True, check_same_thread=False)

        msgmap_file = self.root / "msgmap.sqlite3"
        if not msgmap_file.exists():
            msgmap_file = self.idx_dir / "msgmap.sqlite3"
        self.msgmap_db = (
            sqlite3.connect(f"file:{msgmap_file}?mode=ro", uri=True, check_same_thread=False)
            if msgmap_file.exists()
            else None
        )

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

        # 3. Persistent git cat-file --batch against all.git
        all_git = self.root / "all.git"
        if not all_git.exists() and (self.idx_dir / "all.git").exists():
            all_git = self.idx_dir / "all.git"

        self._cat_proc = (
            subprocess.Popen(
                ["git", "--git-dir", str(all_git), "cat-file", "--batch"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                bufsize=0,
            )
            if all_git.exists()
            else None
        )

    def close(self) -> None:
        """Safely close open databases and processes."""
        if hasattr(self, "over_db") and self.over_db:
            try:
                self.over_db.close()
            except Exception:
                pass
        if hasattr(self, "msgmap_db") and self.msgmap_db:
            try:
                self.msgmap_db.close()
            except Exception:
                pass
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
        cursor = self.over_db.execute(
            "SELECT num, tid, sid, ts, ds, ddd FROM over WHERE num = ? AND num > 0 LIMIT 1",
            (num,),
        )
        row = cursor.fetchone()
        return self._unpack_row(row) if row else None

    def get_by_mid(self, mid: str) -> SmsgRecord | None:
        """Fetch article by RFC822 Message-ID (mid)."""
        clean_mid = mid.strip("<> \t\r\n")
        if not clean_mid:
            return None

        # Check msgmap.sqlite3 if present
        if self.msgmap_db is not None:
            cursor = self.msgmap_db.execute(
                "SELECT num FROM msg_map WHERE mid = ? LIMIT 1", (clean_mid,)
            )
            row = cursor.fetchone()
            if row and row[0] > 0:
                return self.get_by_num(row[0])

        # Fallback for extindex schema (msgid + id2num inside over.sqlite3)
        try:
            cursor = self.over_db.execute(
                """
                SELECT i2n.num FROM msgid m
                JOIN id2num i2n ON m.id = i2n.id
                WHERE m.mid = ? AND i2n.num > 0 LIMIT 1
                """,
                (clean_mid,),
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
        cursor = self.over_db.execute(
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
        cursor = self.over_db.execute(
            """
            SELECT num, tid, sid, ts, ds, ddd
            FROM over
            WHERE num > 0
            ORDER BY ds DESC
            LIMIT 500
            """
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
        if not self._cat_proc or not self._cat_proc.stdin or not self._cat_proc.stdout:
            return {}
        try:
            self._cat_proc.stdin.write(f"{blob_oid}\n".encode("ascii"))
            self._cat_proc.stdin.flush()
            header = self._cat_proc.stdout.readline().decode("ascii").strip()
            if header.endswith("missing"):
                return {}
            parts = header.split()
            if len(parts) < 3:
                return {}
            size = int(parts[2])
            payload = self._cat_proc.stdout.read(size)
            self._cat_proc.stdout.read(1)  # trailing LF

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
                cursor = self.over_db.execute(
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

        cursor = self.over_db.execute(
            "SELECT num, tid, sid, ts, ds, ddd FROM over WHERE num > 0 LIMIT 1000"
        )
        matches: list[SmsgRecord] = []
        for r in cursor.fetchall():
            rec = self._unpack_row(r)
            if rec.git_blob and clean_sha in rec.git_blob.lower():
                matches.append(rec)
        return matches

    def get_cover_letter(self, tid: int) -> SmsgRecord | None:
        """Find the series cover letter [0/N] in a thread."""
        thread = self.get_thread_by_tid(tid)
        for rec in thread:
            if re.search(r"\[[^\]]*\b0+/\d+[^\]]*\]", rec.subject):
                return rec
        return None

