"""webapp/backend/services/mailing_list_service.py - WebApp Mailing List & LKML Service.

Connects the web application to the public-inbox / LKML archive (via in-process
store or remote micro-daemon over SSH tunnel). Provides bounded caching, thread
hierarchies, email beautification, search querying, and graceful offline fallback.
"""
from __future__ import annotations

from collections import OrderedDict
import logging
import re
import threading
import urllib.parse
from typing import Any

from core.config import get_mailing_list_config
from parser.mailing_list.ml_client import MailingListClient

logger = logging.getLogger(__name__)

LORE_BASE_URL = "https://lore.kernelinfo.org/all/"

# Subject decorations stripped when deriving the base subject shared by every
# revision of a patch series: reply prefixes and leading "[PATCH v3 2/5]" tags.
_REPLY_PREFIX_RE = re.compile(r"^\s*(?:re|aw|fw|fwd)\s*:\s*", re.IGNORECASE)
_BRACKET_TAG_RE = re.compile(r"^\s*\[[^\]]*\]\s*")

# Safety bound on how many sibling threads a version lookup will expand.
MAX_VERSION_THREADS = 8


def normalize_series_subject(subject: str) -> str:
    """Base subject of a series: reply prefixes and bracketed patch tags removed."""
    text = str(subject or "")
    while True:
        stripped = _BRACKET_TAG_RE.sub("", _REPLY_PREFIX_RE.sub("", text, count=1), count=1)
        if stripped == text:
            break
        text = stripped
    return re.sub(r"\s+", " ", text).strip()


def _is_series_message(msg: dict[str, Any]) -> bool:
    """True when the message is itself a patch, cover letter or pull request."""
    return bool(msg.get("is_patch") or msg.get("is_cover") or msg.get("is_pull"))


def _subject_matches_series(subject: str, base_subject: str) -> bool:
    """True when a subject belongs to the series identified by base_subject.

    A revision's cover letter (or its single patch) normalizes straight to the base
    subject; the individual patches of a series instead extend it ("<base>: part a"),
    which is only accepted for a base subject long enough to be distinctive.
    """
    if not base_subject:
        return False
    candidate = normalize_series_subject(subject)
    if candidate == base_subject:
        return True
    return len(base_subject) >= 16 and candidate.startswith(base_subject)


class MailingListService:
    """Service providing LKML email and thread access for the WebApp."""

    def __init__(
        self,
        max_email_cache: int = 512,
        max_thread_cache: int = 128,
        max_search_cache: int = 64,
    ) -> None:
        self._client: MailingListClient | None = None
        self._client_init_failed = False
        self._client_init_error = ""
        self._lock = threading.Lock()

        self._max_email_cache = max_email_cache
        self._max_thread_cache = max_thread_cache
        self._max_search_cache = max_search_cache

        self._email_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._thread_cache: OrderedDict[int, dict[str, Any]] = OrderedDict()
        self._search_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._version_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def _get_client(self) -> MailingListClient | None:
        """Lazily initialize client connection to in-process store or daemon."""
        with self._lock:
            if self._client is not None:
                return self._client

            cfg = get_mailing_list_config()
            if not cfg.get("enabled", True):
                self._client_init_error = "Mailing list subsystem is disabled in configuration."
                return None

            try:
                self._client = MailingListClient()
                self._client_init_failed = False
                self._client_init_error = ""
                return self._client
            except Exception as exc:
                self._client_init_failed = True
                self._client_init_error = str(exc)
                logger.warning("Could not initialize MailingListClient: %s", exc)
                return None

    def get_status(self) -> dict[str, Any]:
        """Check status and reachability of the mailing list daemon/archive."""
        cfg = get_mailing_list_config()
        enabled = bool(cfg.get("enabled", True))
        if not enabled:
            return {
                "enabled": False,
                "available": False,
                "reason": "Disabled in configuration",
                "lore_url": LORE_BASE_URL,
            }

        client = self._get_client()
        if client is None:
            return {
                "enabled": True,
                "available": False,
                "reason": self._client_init_error or "Mailing list archive unavailable",
                "lore_url": LORE_BASE_URL,
            }

        is_healthy = False
        try:
            if client._in_process_resolver is not None:
                is_healthy = True
            else:
                is_healthy = client._check_health(timeout=1.5)
        except Exception:
            is_healthy = False

        return {
            "enabled": True,
            "available": is_healthy,
            "use_tunnel": client.use_tunnel,
            "port": client._effective_port,
            "lore_url": LORE_BASE_URL,
            "reason": None if is_healthy else "Mailing list daemon not responding on port",
        }

    def get_message(self, mid_or_num: str | int) -> dict[str, Any]:
        """Fetch full email detail by message-ID or sequential article number."""
        key = urllib.parse.unquote(str(mid_or_num)).strip("<> ")
        if not key:
            return {
                "available": False,
                "error": "Missing message identifier",
                "lore_url": LORE_BASE_URL,
            }

        with self._lock:
            if key in self._email_cache:
                cached = self._email_cache[key]
                if cached.get("available") and not cached.get("error"):
                    self._email_cache.move_to_end(key)
                    return cached

        client = self._get_client()
        if client is None:
            return {
                "available": False,
                "message_id": key,
                "error": self._client_init_error or "Mailing list archive unavailable",
                "lore_url": f"{LORE_BASE_URL}{key}/",
            }

        try:
            detail = client.get_email_detail(key)
            if not detail and " " in key:
                alt_key = key.replace(" ", "+")
                detail = client.get_email_detail(alt_key)
                if detail:
                    key = alt_key

            if not detail:
                return {
                    "available": False,
                    "message_id": key,
                    "error": f"Email '{key}' not found in public-inbox archive",
                    "lore_url": f"{LORE_BASE_URL}{key}/",
                }

            result: dict[str, Any] = {
                "available": True,
                "lore_url": f"{LORE_BASE_URL}{key}/",
                **detail,
            }

            with self._lock:
                self._email_cache[key] = result
                if len(self._email_cache) > self._max_email_cache:
                    self._email_cache.popitem(last=False)

            return result
        except Exception as exc:
            logger.debug("Error retrieving email %s: %s", key, exc)
            return {
                "available": False,
                "message_id": key,
                "error": str(exc),
                "lore_url": f"{LORE_BASE_URL}{key}/",
            }

    def _resolve_tid(self, tid: int, clean_mid: str) -> tuple[int, str]:
        """Resolve a thread ID from an explicit tid or, failing that, a Message-ID hint."""
        if tid > 0 or not clean_mid:
            return tid, clean_mid

        msg = self.get_message(clean_mid)
        if (not msg.get("available") or not msg.get("tid")) and " " in clean_mid:
            clean_mid = clean_mid.replace(" ", "+")
            msg = self.get_message(clean_mid)
        if msg.get("available") and msg.get("tid"):
            tid = int(msg["tid"])
        return tid, clean_mid

    def get_thread(self, tid: int, mid_hint: str = "") -> dict[str, Any]:
        """Fetch all messages in a thread by thread ID or starting Message-ID."""
        clean_mid = urllib.parse.unquote(mid_hint).strip("<> ")
        tid, clean_mid = self._resolve_tid(tid, clean_mid)

        if tid <= 0:
            return {
                "available": False,
                "tid": 0,
                "error": "Thread identifier not found",
                "lore_url": f"{LORE_BASE_URL}{clean_mid}/" if clean_mid else LORE_BASE_URL,
                "total": 0,
                "messages": [],
            }

        with self._lock:
            if tid in self._thread_cache:
                self._thread_cache.move_to_end(tid)
                return self._thread_cache[tid]

        client = self._get_client()
        if client is None:
            return {
                "available": False,
                "tid": tid,
                "error": self._client_init_error or "Mailing list archive unavailable",
                "lore_url": f"{LORE_BASE_URL}{clean_mid}/" if clean_mid else LORE_BASE_URL,
                "total": 0,
                "messages": [],
            }

        try:
            thread_data = client.get_thread(tid)
            result: dict[str, Any] = {
                "available": True,
                "lore_url": f"{LORE_BASE_URL}t/{tid}" if tid else LORE_BASE_URL,
                **thread_data,
            }

            with self._lock:
                self._thread_cache[tid] = result
                if len(self._thread_cache) > self._max_thread_cache:
                    self._thread_cache.popitem(last=False)

            return result
        except Exception as exc:
            logger.debug("Error retrieving thread %d: %s", tid, exc)
            return {
                "available": False,
                "tid": tid,
                "error": str(exc),
                "lore_url": LORE_BASE_URL,
                "total": 0,
                "messages": [],
            }

    @staticmethod
    def _absorb_version_thread(
        entries: dict[int, dict[str, Any]],
        payload: dict[str, Any],
        tid: int,
    ) -> set[int]:
        """Fold one thread's messages into the per-version entries; returns the versions it holds."""
        messages = list(payload.get("messages") or [])
        if not messages:
            return set()

        # Replies whose subject carries no patch tag belong to the revision this
        # thread was opened with, which is the earliest series message in it.
        anchors = [m for m in messages if _is_series_message(m)] or messages
        earliest = min(anchors, key=lambda m: int(m.get("timestamp") or 0))
        dominant = int(earliest.get("patch_version") or 1)

        seen: set[int] = set()
        for msg in messages:
            version = int(msg.get("patch_version") or 1) if _is_series_message(msg) else dominant
            seen.add(version)
            entry = entries.setdefault(version, {
                "version": version,
                "tid": int(msg.get("tid") or tid),
                "subject": msg.get("subject", ""),
                "root_message_id": msg.get("message_id", ""),
                "cover_message_id": "",
                "author": msg.get("author", ""),
                "timestamp": 0,
                "date_iso": "",
                "total": 0,
                "patches": 0,
            })
            entry["total"] += 1
            if _is_series_message(msg):
                entry["patches"] += 1
            if msg.get("is_cover") and not entry["cover_message_id"]:
                entry["cover_message_id"] = msg.get("message_id", "")

            ts = int(msg.get("timestamp") or 0)
            if not entry["timestamp"] or (ts and ts < entry["timestamp"]):
                entry["timestamp"] = ts
                entry["date_iso"] = msg.get("date_iso", "")
                entry["author"] = msg.get("author", "")
                entry["subject"] = msg.get("subject", "")
                entry["root_message_id"] = msg.get("message_id", "")
                entry["tid"] = int(msg.get("tid") or tid)
        return seen

    def get_thread_versions(self, tid: int, mid_hint: str = "") -> dict[str, Any]:
        """List every revision (v1, v2, ...) of the patch series a thread belongs to.

        Revisions of a series are normally posted as separate threads sharing one
        base subject, so the series is reconstructed by normalizing the subject of
        the requested thread and matching it against the archive search index.
        """
        clean_mid = urllib.parse.unquote(mid_hint).strip("<> ")
        tid, clean_mid = self._resolve_tid(tid, clean_mid)

        if tid <= 0:
            return {
                "available": False,
                "tid": 0,
                "error": "Thread identifier not found",
                "base_subject": "",
                "current_version": 0,
                "versions": [],
                "lore_url": LORE_BASE_URL,
            }

        cache_key = f"{tid}:{clean_mid}"
        with self._lock:
            if cache_key in self._version_cache:
                self._version_cache.move_to_end(cache_key)
                return self._version_cache[cache_key]

        thread = self.get_thread(tid)
        if not thread.get("available"):
            return {
                "available": False,
                "tid": tid,
                "error": thread.get("error") or "Thread unavailable",
                "base_subject": "",
                "current_version": 0,
                "versions": [],
                "lore_url": LORE_BASE_URL,
            }

        messages = thread.get("messages") or []
        base_subject = normalize_series_subject(
            thread.get("subject") or (messages[0].get("subject", "") if messages else "")
        )

        entries: dict[int, dict[str, Any]] = {}
        current_versions = self._absorb_version_thread(entries, thread, tid)

        # Sibling threads: other revisions of the same series, found by base subject.
        candidate_tids: list[int] = []
        if base_subject:
            for query in (f's:"{base_subject}"', base_subject):
                found = self.search(query=query, page=1, limit=100)
                rows = found.get("results") or []
                for row in rows:
                    row_tid = int(row.get("tid") or 0)
                    if row_tid <= 0 or row_tid == tid or row_tid in candidate_tids:
                        continue
                    if not _subject_matches_series(row.get("subject", ""), base_subject):
                        continue
                    candidate_tids.append(row_tid)
                    if len(candidate_tids) >= MAX_VERSION_THREADS:
                        break
                if candidate_tids:
                    break

        for other_tid in candidate_tids:
            sibling = self.get_thread(other_tid)
            if sibling.get("available"):
                self._absorb_version_thread(entries, sibling, other_tid)

        versions = sorted(entries.values(), key=lambda e: (e["version"], e["timestamp"]))
        current_version = min(current_versions) if current_versions else 0

        # A Message-ID hint pins the revision precisely, which matters for the rare
        # thread that carries messages of more than one revision.
        if clean_mid and len(current_versions) > 1:
            target = clean_mid.lower()
            for msg in messages:
                mids = [str(x).strip("<> ").lower() for x in (msg.get("mids") or [])]
                if msg.get("message_id"):
                    mids.append(str(msg["message_id"]).strip("<> ").lower())
                if target in mids and _is_series_message(msg):
                    current_version = int(msg.get("patch_version") or current_version)
                    break
        for entry in versions:
            entry["is_current"] = entry["version"] == current_version

        result: dict[str, Any] = {
            "available": True,
            "tid": tid,
            "base_subject": base_subject,
            "current_version": current_version,
            "total_versions": len(versions),
            "versions": versions,
            "lore_url": (
                f"{LORE_BASE_URL}?q={urllib.parse.quote(base_subject)}"
                if base_subject else LORE_BASE_URL
            ),
        }

        with self._lock:
            self._version_cache[cache_key] = result
            if len(self._version_cache) > self._max_thread_cache:
                self._version_cache.popitem(last=False)

        return result

    def search(
        self,
        query: str = "",
        page: int = 1,
        limit: int = 50,
        filter_type: str = "all",
    ) -> dict[str, Any]:
        """Search mailing list messages by query and filter with pagination."""
        clean_q = (query or "").strip()
        p = max(1, page)
        lim = max(1, min(100, limit))
        offset = (p - 1) * lim
        cache_key = f"{clean_q}:{p}:{lim}:{filter_type}"

        with self._lock:
            if cache_key in self._search_cache:
                self._search_cache.move_to_end(cache_key)
                return self._search_cache[cache_key]

        client = self._get_client()
        if client is None:
            return {
                "available": False,
                "query": clean_q,
                "filter_type": filter_type,
                "page": p,
                "limit": lim,
                "total": 0,
                "results": [],
                "error": self._client_init_error or "Mailing list archive unavailable",
                "lore_url": f"{LORE_BASE_URL}?q={clean_q}" if clean_q else LORE_BASE_URL,
            }

        try:
            search_data = client.search_emails(
                query=clean_q,
                limit=lim,
                offset=offset,
                filter_type=filter_type,
            )
            result: dict[str, Any] = {
                "available": True,
                "page": p,
                "lore_url": f"{LORE_BASE_URL}?q={clean_q}" if clean_q else LORE_BASE_URL,
                **search_data,
            }

            with self._lock:
                self._search_cache[cache_key] = result
                if len(self._search_cache) > self._max_search_cache:
                    self._search_cache.popitem(last=False)

            return result
        except Exception as exc:
            logger.debug("Error executing mailing list search '%s': %s", clean_q, exc)
            return {
                "available": False,
                "query": clean_q,
                "filter_type": filter_type,
                "page": p,
                "limit": lim,
                "total": 0,
                "results": [],
                "error": str(exc),
                "lore_url": f"{LORE_BASE_URL}?q={clean_q}" if clean_q else LORE_BASE_URL,
            }


# Singleton service export
mailing_list_service = MailingListService()
