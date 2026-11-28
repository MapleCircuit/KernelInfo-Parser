"""parser/mailing_list/ml_store.py - Direct read-only interface to public-inbox v2 / extindex.

Direct read-only interface to public-inbox SQLite databases (over.sqlite3, msgmap.sqlite3),
sharded Xapian search indices, and all.git blob storage.
"""
from __future__ import annotations

import logging
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any
import urllib.parse
import zlib

from db_engine import DBError, ForeignReader, MissingTableError, open_foreign_sqlite
from db_engine.spec import Hex, Join, gt, le
from parser.mailing_list.ml_types import SmsgRecord

try:
    import xapian
    HAS_XAPIAN = True
except ImportError:
    xapian = None  # type: ignore[assignment]
    HAS_XAPIAN = False

logger = logging.getLogger(__name__)

# Columns of an `over` row unpacked by `_unpack_row`
_OVER_COLS = ["num", "tid", "sid", "ts", "ds", "ddd"]

# A Date: header set in the future (a skewed sender clock or a forged header) sorts
# ahead of every real message, so the chronological browse listing (an empty query)
# stops at the present. A clock an hour fast is tolerated; an explicit search query
# is never filtered, so those messages stay findable.
FUTURE_DATE_SKEW_SECONDS = 3600

# Memory ceilings. The archive holds tens of millions of messages, so any query whose
# result set is proportional to the archive (or to how deep a client pages) must be
# bounded: one unbounded fetch is enough to exhaust a shared host's RAM.
MAX_THREAD_MESSAGES = 1000      # messages unpacked for a single thread
MAX_SEARCH_LIMIT = 200          # page size accepted from a caller
MAX_SEARCH_OFFSET = 5_000_000   # how deep paging may go before it is clamped
MAX_SEARCH_SCAN = 20_000        # rows scanned by the non-Xapian substring fallback; Xapian docs walked per request
MAX_FILTERED_SEARCH_SCAN = 2_000  # Xapian docs walked per request when a filter_type needs each record resolved
_XAPIAN_CHUNK_MAX = 500         # docs fetched per Xapian get_mset call during a de-duplicating scan
# Plain-text search detection: words for the Subject tier, and Xapian operator syntax that opts out of it.
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_QUERY_SYNTAX_RE = re.compile(r"(?:^|\s)(?:AND|OR|NOT|XOR|NEAR|ADJ)(?:\s|$)|\.\.|[()]")
MAX_RAW_EMAIL_BYTES = 32 << 20  # single blob read from `git cat-file --batch`
SQLITE_CACHE_KIB = 8_000        # page cache per connection (thread-local handles)


_REPLY_TAG_RE = re.compile(r"^\s*(?:(?:re|fwd?|aw)\s*:\s*|\[[^\]]*\]\s*)+", re.IGNORECASE)


def subject_match_tier(query: str, subject: str) -> int:
    """Rank of a Subject against a plain-text query: 0 exact, 1 phrase contained, 2 all words, 3 other.

    `Re:` / `Fwd:` and bracketed `[PATCH v2 1/3]` tags are ignored and case and runs of
    non-word characters do not matter, so `[PATCH] ext2: mark as deprecated` is an exact
    match for `ext2: mark as deprecated`.
    """
    q_words = _WORD_RE.findall((query or "").lower())
    if not q_words:
        return 3
    s_words = _WORD_RE.findall(_REPLY_TAG_RE.sub("", subject or "").lower())
    if s_words == q_words:
        return 0
    n = len(q_words)
    if any(s_words[i : i + n] == q_words for i in range(len(s_words) - n + 1)):
        return 1
    return 2 if set(q_words) <= set(s_words) else 3


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
    # Public-inbox indexes terms under field prefixes (S=Subject, XNQ=non-quoted body,
    # XDFN=diff filename, A=Author); unprefixed user terms must expand across all of them
    # (plus "" for synthetic unprefixed test shards).
    DEFAULT_PROB_PREFIXES = ("", "S", "XNQ", "XDFN", "A")

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

        # Read-only foreign SQLite readers (one lazily opened connection per thread, PRAGMAs set by db_engine)
        self.over_db: ForeignReader = open_foreign_sqlite(self._over_path, immutable=False, cache_kib=SQLITE_CACHE_KIB)
        self.msgmap_db: ForeignReader | None = (
            open_foreign_sqlite(self._msgmap_path, immutable=False, cache_kib=SQLITE_CACHE_KIB)
            if self._msgmap_path else None
        )
        self._cat_lock = threading.Lock()
        self._qp_lock = threading.Lock()

        # 2. Open all Xapian shards into a unified Database handle
        self.xap_db: Any = None
        self.qp: Any = None
        self.shard_count = 0
        self.shard_errors: list[str] = []

        if HAS_XAPIAN and xapian is not None:
            shard_dirs = sorted(
                [p for p in self.idx_dir.iterdir() if p.is_dir() and p.name.isdigit()],
                key=lambda p: int(p.name),
            )
            if shard_dirs:
                combined_db = xapian.Database()
                loaded_shards = 0
                for sd in shard_dirs:
                    try:
                        combined_db.add_database(xapian.Database(str(sd)))
                        loaded_shards += 1
                    except Exception as exc:  # noqa: BLE001
                        err_msg = f"shard {sd.name} ({sd}): {exc}"
                        self.shard_errors.append(err_msg)
                        logger.warning("Failed to open Xapian %s", err_msg)
                self.shard_count = loaded_shards
                if loaded_shards > 0:
                    self.xap_db = combined_db
                    self.qp = xapian.QueryParser()
                    self.qp.set_database(self.xap_db)
                    self.qp.set_stemmer(xapian.Stem("english"))
                    self.qp.set_stemming_strategy(xapian.QueryParser.STEM_SOME)
                    self.qp.set_default_op(xapian.Query.OP_AND)
                    for x_pfx in self.DEFAULT_PROB_PREFIXES:
                        self.qp.add_prefix("", x_pfx)
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

    def check_integrity(self, *, full: bool = False) -> dict[str, Any]:
        """Run a lightweight read-only integrity check over SQLite overview files and Xapian shards."""
        over_ok, over_status = self.over_db.quick_check(full=full)
        msgmap_info: dict[str, Any] | None = None
        msgmap_ok = True
        if self.msgmap_db is not None and self._msgmap_path is not None:
            msgmap_ok, msgmap_status = self.msgmap_db.quick_check(full=full)
            msgmap_info = {
                "path": str(self._msgmap_path),
                "ok": msgmap_ok,
                "status": msgmap_status,
            }
        shard_Errors = list(getattr(self, "shard_errors", ()))
        ok = bool(over_ok and msgmap_ok and not shard_Errors)
        return {
            "ok": ok,
            "over_sqlite": {
                "path": str(self._over_path),
                "ok": over_ok,
                "status": over_status,
            },
            "msgmap_sqlite": msgmap_info,
            "xapian_shards": int(getattr(self, "shard_count", 0)),
            "shard_errors": shard_Errors,
        }

    def close_thread_handles(self) -> None:
        """Close this thread's SQLite handles.

        Handles are thread-local and the daemon serves each request on a new thread, so
        without this every request would leave two open connections (and their page
        caches) behind for the life of the process.
        """
        self.over_db.close_thread()
        if self.msgmap_db is not None:
            self.msgmap_db.close_thread()

    def close(self) -> None:
        """Safely close open databases and processes."""
        self.over_db.close()
        if self.msgmap_db is not None:
            self.msgmap_db.close()

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
        row = self.over_db.select_one("over", _OVER_COLS, {"num": num})
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

        # Check msgmap.sqlite3 if present
        if self.msgmap_db is not None:
            row = self.msgmap_db.select_one("msg_map", ["num"], {"mid": candidates})
            if row and row[0] > 0:
                return self.get_by_num(row[0])

        # Fallback for extindex schema (msgid + id2num inside over.sqlite3)
        try:
            row = self.over_db.select_one(
                ("msgid", "m"),
                ["i2n.num"],
                {"m.mid": candidates, "i2n.num": gt(0)},
                joins=[Join(("id2num", "i2n"), {"i2n.id": "m.id"})],
            )
            if row and row[0] > 0:
                return self.get_by_num(row[0])
        except DBError:
            pass
        return None

    def count_thread_messages(self, tid: int) -> int:
        """Number of non-ghost messages in a thread (indexed count, no row unpacking)."""
        if tid <= 0:
            return 0
        try:
            return int(self.over_db.count_rows("over", {"tid": tid, "num": gt(0)}))
        except Exception as exc:
            logger.debug("Thread count failed for tid %d: %s", tid, exc)
            return 0

    def get_thread_by_tid(
        self, tid: int, limit: int = MAX_THREAD_MESSAGES, offset: int = 0
    ) -> list[SmsgRecord]:
        """Fetch non-ghost messages in a thread, chronologically and bounded.

        A thread is unbounded in principle (this archive's largest holds over 12,000
        messages), and every row carries a compressed overview blob, so the fetch is
        capped. Series reconstruction only needs the cover letter and the patch's
        siblings, which sit at the start of the thread.
        """
        if tid <= 0:
            return []
        capped = max(1, min(int(limit), MAX_THREAD_MESSAGES))
        rows = self.over_db.select_rows(
            "over", _OVER_COLS, {"tid": tid, "num": gt(0)}, order_by=["ds", "num"],
            limit=capped, offset=max(0, int(offset)),
        )
        if len(rows) >= capped:
            logger.debug("Thread %d truncated at %d messages", tid, capped)
        return [self._unpack_row(r) for r in rows]

    def search_over_subjects(
        self, pattern: str, limit: int = 20, scan_limit: int = 500, offset: int = 0
    ) -> list[SmsgRecord]:
        """Query over table by subject substring pattern (fallback when Xapian unavailable).

        `scan_limit` bounds how many of the newest rows are decompressed and tested, so
        a deep page of results can still be reached without scanning the whole archive.
        Rows are consumed one at a time and only matches inside the requested page are
        retained, so memory stays proportional to `limit` rather than to the scan.
        """
        cursor = self.over_db.iter_rows(
            "over", _OVER_COLS, {"num": gt(0)}, order_by=["-ds"],
            limit=max(500, min(int(scan_limit), MAX_SEARCH_SCAN)),
        )
        matches: list[SmsgRecord] = []
        pattern_lower = pattern.lower()
        skip = max(0, int(offset))
        seen = 0
        for r in cursor:
            rec = self._unpack_row(r)
            if pattern_lower not in rec.subject.lower():
                continue
            seen += 1
            if seen <= skip:
                continue
            matches.append(rec)
            if len(matches) >= limit:
                break
        return matches

    def parse_query(self, query_string: str, flags: int | None = None) -> Any:
        """Parse a query string into a Xapian query under the Xapian lock.

        `xapian.QueryParser` holds a reference to the shared `Database`, which is not
        thread-safe, so parsing must be serialized along with searching.
        """
        if self.qp is None or not HAS_XAPIAN or xapian is None:
            return None
        with self._qp_lock:
            if flags is None:
                return self.qp.parse_query(query_string)
            return self.qp.parse_query(query_string, flags)

    def execute_xapian(self, query: Any, limit: int = 50) -> list[SmsgRecord]:
        """Execute a Xapian query with automatic reopen() on concurrent writer updates.

        A `xapian.Database` is not thread-safe: concurrent searches on the shared handle
        abort the process inside libxapian. All searching is therefore serialized on
        `_qp_lock`, and only the matched message IDs are collected while it is held, so
        the overview lookups that resolve them run outside the critical section.
        """
        return self.search_xapian_page(query, limit=limit)[0]

    def search_xapian_page(
        self, query: Any, limit: int = 50, offset: int = 0, *, ranked: bool = False, check_at_least: int = 0
    ) -> tuple[list[SmsgRecord], int]:
        """Fetch one page of Xapian matches plus the estimated total match count.

        The offset is applied by Xapian, so a deep page costs what the first one does
        instead of materializing every record skipped along the way. `ranked` orders by
        BM25 relevance with newest-first as the tie-break (default: newest first only);
        `check_at_least` makes the match count exact up to that many matches. A
        Message-ID indexed in several shards (cross-posts) is returned once.
        """
        if self.xap_db is None or not HAS_XAPIAN or xapian is None or query is None:
            return [], 0

        mids: list[str] = []
        estimated = 0
        for attempt in range(2):
            try:
                with self._qp_lock:
                    enquire = xapian.Enquire(self.xap_db)
                    enquire.set_query(query)
                    if ranked:
                        enquire.set_weighting_scheme(xapian.BM25Weight())
                        enquire.set_sort_by_relevance_then_value(0, True)
                    else:
                        enquire.set_weighting_scheme(xapian.BoolWeight())
                        enquire.set_sort_by_value(0, True)
                    # Xapian applies the offset itself, so a deep page costs the same
                    # as the first one instead of materializing everything skipped.
                    if check_at_least > 0:
                        mset = enquire.get_mset(max(0, offset), max(1, limit), int(check_at_least))
                    else:
                        mset = enquire.get_mset(max(0, offset), max(1, limit))
                    estimated = mset.get_matches_estimated()

                    for match in mset:
                        for term_item in match.document.termlist():
                            t = term_item.term.decode("utf-8", errors="replace")
                            if t.startswith("Q"):
                                mids.append(t[1:])
                                break
                break
            except getattr(xapian, "DatabaseModifiedError", Exception):
                mids.clear()
                if attempt == 0:
                    with self._qp_lock:
                        self.xap_db.reopen()
                else:
                    raise

        results: list[SmsgRecord] = []
        seen: set[str] = set()
        for mid in mids:
            if mid in seen:
                continue
            seen.add(mid)
            rec = self.get_by_mid(mid)
            if rec:
                results.append(rec)
        return results, estimated

    def subject_tier_query(self, query_string: str) -> Any:
        """Xapian query requiring every word of a plain-text search in the Subject (or None).

        Only plain text qualifies: a query using a search prefix (`s:`, `f:`, `m:`, ...),
        quotes or boolean operators is the caller's own Xapian syntax and is run as given.
        """
        text = (query_string or "").strip()
        if not text or '"' in text or _QUERY_SYNTAX_RE.search(text):
            return None
        prefixes = {*self.BOOLEAN_PREFIXES, *self.PROB_PREFIXES}
        if any(re.search(rf"(?<![\w]){re.escape(p)}:\S", text) for p in prefixes):
            return None
        words = _WORD_RE.findall(text)
        if not words:
            return None
        return self.parse_query(" ".join(f"s:{w}" for w in words))

    def _mset_mids(
        self, query: Any, ranked: bool, start: int, size: int
    ) -> tuple[list[str | None], int]:
        """One mset slice as per-document Message-IDs (cheap termlist `Q` walk) plus the match estimate."""
        for attempt in range(2):
            mids: list[str | None] = []
            try:
                with self._qp_lock:
                    enquire = xapian.Enquire(self.xap_db)
                    enquire.set_query(query)
                    if ranked:
                        enquire.set_weighting_scheme(xapian.BM25Weight())
                        enquire.set_sort_by_relevance_then_value(0, True)
                    else:
                        enquire.set_weighting_scheme(xapian.BoolWeight())
                        enquire.set_sort_by_value(0, True)
                    mset = enquire.get_mset(max(0, start), max(1, size))
                    estimated = mset.get_matches_estimated()
                    for match in mset:
                        found: str | None = None
                        for term_item in match.document.termlist():
                            t = term_item.term.decode("utf-8", errors="replace")
                            if t.startswith("Q"):
                                found = t[1:]
                                break
                        mids.append(found)
                return mids, estimated
            except getattr(xapian, "DatabaseModifiedError", Exception):
                if attempt == 0:
                    with self._qp_lock:
                        self.xap_db.reopen()
                else:
                    raise
        return [], 0

    @staticmethod
    def _filter_accepts(filter_type: str, subject: str) -> bool:
        """True when a subject passes the `filter_type` (all / patch / cover / pull)."""
        low = subject.lower()
        if filter_type == "patch":
            return bool(re.search(r"\[[^\]]*\bpatch\b", low))
        if filter_type == "cover":
            return bool(re.search(r"\[[^\]]*\b0+/\d+[^\]]*\]", low))
        if filter_type == "pull":
            return bool(re.search(r"\[[^\]]*\bgit pull\b", low))
        return True

    def search_xapian_unique(
        self, query_string: str, limit: int, offset: int, filter_type: str = "all"
    ) -> dict[str, Any]:
        """Page of one Message-ID-unique, deterministic Xapian result sequence.

        The sequence is the de-duplicated concatenation of the tiers: for plain text,
        tier 1 (Subject holds every word, BM25 then newest first) and tier 2 (every other
        match, same order); for a query in Xapian syntax, a single newest-first tier. A
        Message-ID indexed as several documents (cross-posts) occupies one position, at
        its first appearance, and a Message-ID emitted by tier 1 is skipped in tier 2.
        Ties break by Xapian docid, so repeated requests walk the same sequence.

        Exact zone: the page is positions `[offset, offset + limit)` of that sequence,
        found by one walk from the top. Skipped documents cost only a termlist `Q` read;
        only the returned page is resolved through `get_by_mid` (with a `filter_type`
        every candidate is resolved to test its subject and counts only if it passes).
        One extra unique item is probed to decide `has_more`. A position is counted when
        its Message-ID is first seen, in the skipped prefix and the page alike, so an
        unresolvable Message-ID occupies its position (the page may be one short) and
        page boundaries never move. The walk covers at most `MAX_SEARCH_SCAN` documents
        (`MAX_FILTERED_SEARCH_SCAN` with a filter).

        Approximate zone: when the window lies beyond that cap, the walk stops at
        document `s` of tier `t` with `u` unique positions counted. Position `p >= u`
        maps to Xapian document offset `s + (p - u)` in tier `t` (the tail is assumed
        duplicate-free; a later tier uses the offset left after subtracting the earlier
        tiers' estimated sizes). That window is fetched natively, in chunks bounded by
        `2 * 500` documents, de-duplicated against the Message-IDs seen and within the
        fetched chunks only, and `scan_capped` is true. `has_more` is true when a
        unique item exists past the page, or when the last native chunk was full and
        the page is non-empty. A cap that lands exactly at the end of the results is
        not flagged. `has_more` is never true for an empty page.

        Returns `records`, `has_more`, `estimated` (sum of every tier's Xapian match
        estimate, stable across pages), `tiered` and `scan_capped`.
        """
        out: dict[str, Any] = {"records": [], "has_more": False, "estimated": 0, "tiered": False, "scan_capped": False}
        full = self.parse_query(query_string)
        if full is None or xapian is None:
            return out
        subject_q = self.subject_tier_query(query_string)
        if subject_q is None:
            tiers: list[tuple[Any, bool]] = [(full, False)]
        else:
            tiers = [
                (subject_q, True),
                (xapian.Query(xapian.Query.OP_AND_NOT, full, subject_q), True),
            ]
            out["tiered"] = True
        filtered = filter_type != "all"
        cap = MAX_FILTERED_SEARCH_SCAN if filtered else MAX_SEARCH_SCAN
        want = limit + 1  # one probe past the page
        est = [self._mset_mids(q, r, 0, 1)[1] for q, r in tiers]
        out["estimated"] = sum(est)
        seen: set[str] = set()
        records: list[SmsgRecord] = []
        st = {"position": 0, "taken": 0}  # positions counted; page items (incl. probe) taken

        def consume(mid: str, exact: bool) -> bool:
            """Count one unique Message-ID; True once the page and its probe are complete."""
            if exact and not filtered and st["position"] < offset:
                st["position"] += 1
                return False
            rec = self.get_by_mid(mid)
            if filtered and (rec is None or not self._filter_accepts(filter_type, rec.subject)):
                return False
            st["position"] += 1
            if exact and st["position"] <= offset:
                return False
            st["taken"] += 1
            if rec is not None and st["taken"] <= limit:
                records.append(rec)
            return st["taken"] >= want

        scanned = 0
        done = False
        capped_at: tuple[int, int] | None = None
        for ti, (query, ranked) in enumerate(tiers):
            start = 0
            while not done and capped_at is None:
                if filtered:
                    size = min(_XAPIAN_CHUNK_MAX, max(16, want * 4))
                else:
                    size = min(_XAPIAN_CHUNK_MAX, max(16, offset + want - st["position"]))
                size = min(size, cap - scanned)
                if size <= 0:
                    capped_at = (ti, start)
                    break
                mids, _est = self._mset_mids(query, ranked, start, size)
                if not mids:
                    break
                start += len(mids)
                scanned += len(mids)
                for mid in mids:
                    if mid is None or mid in seen:
                        continue
                    seen.add(mid)
                    if consume(mid, True):
                        done = True
                        break
                if len(mids) < size:
                    break

        more_native = False
        if capped_at is not None and not done:
            ti, start = capped_at
            native = start + max(0, offset - st["position"])
            chunk = min(_XAPIAN_CHUNK_MAX, max(16, want * 4))
            fetched = 0
            while ti < len(tiers) and not done and fetched < 2 * _XAPIAN_CHUNK_MAX:
                query, ranked = tiers[ti]
                mids, _est = self._mset_mids(query, ranked, native, chunk)
                if not mids:
                    native = max(0, native - est[ti]) if native > 0 else 0
                    ti += 1
                    more_native = False
                    continue
                out["scan_capped"] = True
                fetched += len(mids)
                native += len(mids)
                for mid in mids:
                    if mid is None or mid in seen:
                        continue
                    seen.add(mid)
                    if consume(mid, False):
                        done = True
                        break
                more_native = len(mids) >= chunk
                if not more_native:
                    ti += 1
                    native = 0

        has_more = st["taken"] > limit or (out["scan_capped"] and more_native and bool(records))
        out["records"] = records
        out["has_more"] = bool(has_more and out["records"])
        return out

    @staticmethod
    def _read_cat_payload(proc: Any, size: int, keep: int) -> bytes:
        """Read one `git cat-file --batch` payload, keeping at most `keep` bytes.

        The declared size is validated before allocating: a desynchronized stream makes
        the header line garbage, and a bogus size would otherwise be handed straight to
        `read()` as a multi-gigabyte allocation. The remainder of a payload larger than
        `keep` is drained in chunks so the stream stays in sync without being held in
        memory, and the trailing newline is consumed.
        """
        if size < 0 or size > MAX_RAW_EMAIL_BYTES:
            raise ValueError(f"implausible blob size {size} from git cat-file")

        kept = bytearray()
        remaining = size
        while remaining > 0:
            chunk = proc.stdout.read(min(remaining, 1 << 20))
            if not chunk:
                raise OSError("git cat-file stream ended mid-payload")
            remaining -= len(chunk)
            if len(kept) < keep:
                kept += chunk[: keep - len(kept)]
        proc.stdout.read(1)  # trailing LF
        return bytes(kept)

    # Aliases and helper lookups
    lookup_by_mid = get_by_mid
    lookup_thread = get_thread_by_tid

    def lookup_by_blob(self, blob_sha: str) -> list[SmsgRecord]:
        """Fetch all messages matching a git blob SHA in their overview data."""
        clean_sha = blob_sha.strip().lower()
        if len(clean_sha) == 40:
            try:
                raw_bin = bytes.fromhex(clean_sha)
                rows = self.over_db.select_rows("xref3", ["docid"], {"oidbin": raw_bin}, limit=10)
                docids = [row[0] for row in rows if row[0] > 0]
                if docids:
                    matches: list[SmsgRecord] = []
                    for docid in docids:
                        rec = self.get_by_num(docid)
                        if rec:
                            matches.append(rec)
                    return matches
            except MissingTableError:
                pass
            except Exception as exc:
                logger.debug("xref3 lookup error for blob %s: %s", clean_sha, exc)

        # The indexed `xref3` lookup above is authoritative wherever the archive is
        # indexed. This recency scan can only ever match the newest messages, and it
        # decompresses an overview blob per row (hundreds of random reads per commit on
        # a multi-gigabyte archive), so it runs only without a Xapian index.
        if HAS_XAPIAN and self.xap_db is not None:
            return []

        cursor = self.over_db.iter_rows("over", _OVER_COLS, {"num": gt(0)}, order_by=["-ds"], limit=1000)
        matches: list[SmsgRecord] = []
        for r in cursor:
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
                payload = self._read_cat_payload(proc, int(parts[2]), keep=MAX_RAW_EMAIL_BYTES)
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
                row = self.over_db.select_one("xref3", {"h": Hex("oidbin")}, {"docid": rec.num})
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
            "commit_message": patch_info.get("commit_message"),
            "commentary": patch_info.get("commentary"),
            "diffstat": patch_info.get("diffstat"),
            "base_commit": patch_info.get("base_commit"),
            "review_trailers": patch_info.get("review_trailers", []),
            "trailers": patch_info.get("review_trailers", []),
            "patch_version": patch_v,
            "patch_idx": patch_idx,
            "patch_total": patch_total,
        }

    def get_thread_messages(
        self, tid: int, limit: int = MAX_THREAD_MESSAGES, offset: int = 0
    ) -> dict[str, Any]:
        """Fetch one page of a thread's messages with summary metadata.

        `total` is the thread's real message count (an indexed count), while `messages`
        is bounded, so a 12,000-message thread cannot be unpacked into the response.
        """
        records = self.get_thread_by_tid(tid, limit=limit, offset=offset)
        if not records:
            return {
                "tid": tid, "subject": "", "total": 0, "participants": [],
                "messages": [], "limit": limit, "offset": offset, "truncated": False,
            }
        total = self.count_thread_messages(tid) or len(records)

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
            "total": total,
            "participants": sorted(list(participants)),
            "messages": messages,
            "limit": limit,
            "offset": offset,
            "truncated": (offset + len(messages)) < total,
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
        # The offset is pushed down into Xapian / SQLite instead of being applied to a
        # materialized list: fetching `limit + offset` records cost ~3GB at offset 2M on
        # a 22M-message archive, which is what exhausted the host's RAM.
        limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
        offset = max(0, min(int(offset), MAX_SEARCH_OFFSET))
        matched_records: list[SmsgRecord] = []
        estimated_total = 0
        has_more = False
        scan_capped = False

        # 1. Xapian: one Message-ID-unique, deterministic sequence; the page is a slice of it
        # (filter_type applied inside the walk, before counting).
        xapian_used = False
        xapian_error: str | None = None
        shard_errors = list(getattr(self, "shard_errors", ()))
        if clean_q and self.xap_db is not None and HAS_XAPIAN and self.qp is not None and xapian is not None:
            try:
                found = self.search_xapian_unique(clean_q, limit, offset, filter_type)
                matched_records = found["records"]
                estimated_total = int(found["estimated"])
                has_more = bool(found["has_more"])
                scan_capped = bool(found["scan_capped"])
                xapian_used = True
            except Exception as exc:
                xapian_error = str(exc)
                logger.warning("Xapian query error for %r: %s", clean_q, exc)
        elif clean_q and shard_errors and not self.xap_db:
            xapian_error = "; ".join(shard_errors)

        # 2. Fallback to over.sqlite3 substring search / browse listing when Xapian was not used
        if not xapian_used:
            fetch = limit + 1 if filter_type == "all" else min(limit * 4, MAX_SEARCH_LIMIT * 4)
            if clean_q:
                matched_records = self.search_over_subjects(
                    clean_q, limit=fetch, scan_limit=MAX_SEARCH_SCAN, offset=offset
                )
                if xapian_error and not matched_records:
                    raise RuntimeError(f"Primary Xapian search failed: {xapian_error}")
            else:
                # Empty query: recent messages ordered chronologically, future dates excluded.
                # A plain `ds <=` bound keeps the walk on `idx_ds` (newest first, stops at
                # `limit`); adding `ds IS NULL OR` makes SQLite sort the whole 22M-row table
                # (~20s). Only ghost rows (`num <= 0`) lack a `ds`, and `num > 0` drops them anyway.
                rows = self.over_db.select_rows(
                    "over", _OVER_COLS,
                    [{"num": gt(0)}, {"ds": le(int(time.time()) + FUTURE_DATE_SKEW_SECONDS)}],
                    order_by=["-ds"], limit=fetch, offset=offset,
                )
                matched_records = [self._unpack_row(r) for r in rows]
            raw_count = len(matched_records)
            matched_records = [r for r in matched_records if self._filter_accepts(filter_type, r.subject)]
            has_more = len(matched_records) > limit or raw_count >= fetch
            matched_records = matched_records[:limit]

        # `total` is Xapian's estimate when available, never below what the pages already show.
        page = matched_records
        total = max(estimated_total, offset + len(page) + (1 if has_more else 0))

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

        out_payload: dict[str, Any] = {
            "query": clean_q,
            "filter_type": filter_type,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": has_more,
            "results": results,
        }
        if scan_capped:
            out_payload["scan_capped"] = True
        if xapian_error or shard_errors:
            out_payload["degraded"] = True
            out_payload["error"] = (
                f"Primary Xapian search failed ({xapian_error}); showing recent overview fallback only"
                if xapian_error
                else f"Xapian shard errors: {'; '.join(shard_errors)}"
            )
        return out_payload


