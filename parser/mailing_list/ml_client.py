"""parser/mailing_list/ml_client.py - Client interface to Mailing List Resolver.

Handles direct in-process queries, SSH tunnel connection management, remote daemon
auto-launching, batch HTTP requests, and TableEngine staging into m_commit_email.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import time
from typing import Any
import urllib.error
import urllib.request

from core.config import get_mailing_list_config, get_ssh_tunnel_config
from core.ssh_tunnel import is_ssh_tunnel_active, start_ssh_tunnel
from parser.mailing_list.ml_resolver import KernelCommitEmailResolver
from parser.mailing_list.ml_store import PublicInboxLocalStore
from parser.mailing_list.ml_types import (
    CommitFeatures,
    MLMatchTier,
    MLRelationType,
    ResolvedCommitContext,
)

logger = logging.getLogger(__name__)


DAEMON_VERSION = "1.4.1"


class MailingListClient:
    """Client for resolving kernel commits to mailing list emails."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        git_dir: str | Path | None = None,
        storage_dir: str | None = None,
    ):
        self.cfg = config or get_mailing_list_config()
        self.git_dir = str(git_dir) if git_dir else ""
        self.storage_dir = storage_dir or self.cfg.get("storage_dir", "/srv/lore")
        self.port = int(self.cfg.get("port", 8192))
        self.batch_size = max(1, int(self.cfg.get("batch_size", 200)))
        self.auto_start = bool(self.cfg.get("auto_start_daemon", True))
        self.tunnel_cfg = get_ssh_tunnel_config("mailing_list")
        self.use_tunnel = bool(self.tunnel_cfg.get("enabled", False))

        self._in_process_resolver: KernelCommitEmailResolver | None = None
        self._effective_port = self.port

        try:
            self._init_backend()
        except Exception:
            self.close()
            raise

    def _init_backend(self) -> None:
        """Initialize in-process resolver if co-located, or configure SSH tunnel."""
        # 1. Co-located mode: local storage directory exists directly on this machine
        if not self.use_tunnel:
            local_path = Path(self.storage_dir)
            if local_path.exists():
                try:
                    logger.info("Initializing in-process PublicInboxLocalStore at %s", self.storage_dir)
                    store = PublicInboxLocalStore(self.storage_dir)
                    self._in_process_resolver = KernelCommitEmailResolver(store, kernel_git_dir=self.git_dir)
                    return
                except Exception as exc:
                    raise RuntimeError(
                        f"Failed to initialize local mailing list store at '{self.storage_dir}': {exc}"
                    ) from exc
            else:
                raise FileNotFoundError(
                    f"No Linux kernel mailing list directory or tools found at local directory '{self.storage_dir}' and SSH tunnel is disabled. "
                    "Configure a valid local archive directory via --ml-storage-dir or enable 'mailing_list.ssh_tunnel' in config.json."
                )

        # 2. Remote / Tunnel mode
        if self.use_tunnel:
            if not is_ssh_tunnel_active("mailing_list"):
                logger.info("Starting SSH tunnel for mailing list...")
                tunnel = start_ssh_tunnel(self.tunnel_cfg, feature="mailing_list")
                self._effective_port = tunnel.local_port
            else:
                from core.ssh_tunnel import get_active_ssh_tunnel
                act = get_active_ssh_tunnel("mailing_list")
                self._effective_port = act.local_port if act else self.port

            if self.auto_start:
                self._ensure_remote_daemon_running()
            elif not self._check_health(timeout=2.0):
                raise ConnectionError(
                    f"Mailing list daemon is not responding on 127.0.0.1:{self._effective_port}. "
                    "Ensure the daemon is running on the remote host or enable 'auto_start_daemon'."
                )

    def _check_health(self, timeout: float = 2.0) -> bool:
        """Check if daemon is responding to /health on 127.0.0.1:<effective_port>."""
        url = f"http://127.0.0.1:{self._effective_port}/health"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data.get("status") == "ok" and data.get("version") == DAEMON_VERSION
        except Exception:
            return False
        return False

    def _ensure_remote_daemon_running(self) -> None:
        """Probe remote daemon health; if inactive, sync package and launch remotely via SSH."""
        if self._check_health(timeout=1.5):
            logger.info("Mailing list daemon is healthy on port %d", self._effective_port)
            return

        logger.info("Remote mailing list daemon not reachable on port %d; attempting remote startup...", self._effective_port)
        host = self.tunnel_cfg.get("host", "")
        ssh_port = self.tunnel_cfg.get("port", 22)
        user = self.tunnel_cfg.get("user", "")
        key_file = self.tunnel_cfg.get("key_file", "")
        remote_port = self.tunnel_cfg.get("remote_port", 8192)

        if not host:
            raise ValueError("Cannot auto-start daemon: 'host' not configured in mailing_list.ssh_tunnel.")

        target = f"{user}@{host}" if user else host
        key_path = str(Path(os.path.expanduser(key_file)).resolve()) if key_file else None

        # 1. Sync parser/mailing_list package to remote host so daemon code is guaranteed present
        try:
            local_pkg = Path(__file__).resolve().parent
            scp_cmd = ["scp", "-P", str(ssh_port), "-o", "BatchMode=yes"]
            ssh_prep = ["ssh", "-p", str(ssh_port), "-o", "BatchMode=yes"]
            if key_path:
                scp_cmd.extend(["-i", key_path])
                ssh_prep.extend(["-i", key_path])

            subprocess.run(
                ssh_prep + [target, "mkdir -p ~/KernelInfo-Parser/parser"],
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            scp_cmd.extend(["-r", str(local_pkg), f"{target}:~/KernelInfo-Parser/parser/"])
            subprocess.run(scp_cmd, timeout=8, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except Exception as exc:
            logger.debug("Optional scp sync of parser/mailing_list to remote host: %s", exc)

        # 2. Command to stop stale daemon on port and start new daemon in background on remote host
        remote_script = (
            f"fuser -k {remote_port}/tcp >/dev/null 2>&1 || true; "
            f"cd ~/KernelInfo-Parser && "
            f"nohup python3 -m parser.mailing_list.ml_server --storage-dir \"{self.storage_dir}\" "
            f"--port {remote_port} >/tmp/ml_server.log 2>&1 </dev/null &"
        )
        cmd = ["ssh", "-f", "-n", "-p", str(ssh_port), "-o", "BatchMode=yes"]
        if key_path:
            cmd.extend(["-i", key_path])
        cmd.extend([target, remote_script])

        try:
            subprocess.run(cmd, timeout=6, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except Exception as exc:
            logger.warning("Remote daemon startup command failed: %s", exc)

        # 3. Wait for daemon to become healthy
        start_t = time.time()
        while time.time() - start_t < 8.0:
            time.sleep(0.5)
            if self._check_health(timeout=1.0):
                logger.info("Remote mailing list daemon successfully initialized on port %d!", self._effective_port)
                return

        # Fetch remote log tail if startup timed out
        remote_log = ""
        try:
            log_cmd = ["ssh", "-p", str(ssh_port), "-o", "BatchMode=yes"]
            if key_path:
                log_cmd.extend(["-i", key_path])
            log_cmd.extend([target, "tail -n 10 /tmp/ml_server.log 2>/dev/null"])
            log_res = subprocess.run(log_cmd, timeout=5, capture_output=True, text=True, check=False)
            if log_res.stdout.strip():
                remote_log = f"\nRemote log (/tmp/ml_server.log):\n{log_res.stdout.strip()}"
        except Exception:
            pass

        raise ConnectionError(
            f"Remote mailing list daemon could not be reached on {target}:{remote_port} via port {self._effective_port}.{remote_log}"
        )

    def resolve_commits(
        self, commits: list[CommitFeatures]
    ) -> dict[str, ResolvedCommitContext]:
        """Resolve a list of commit features against the mailing list archive."""
        if not commits:
            return {}

        # 1. In-process direct resolution
        if self._in_process_resolver is not None:
            return self._in_process_resolver.resolve_batch(commits)

        if not self.use_tunnel and not self._in_process_resolver:
            raise FileNotFoundError(
                f"No Linux kernel mailing list archive found at local path '{self.storage_dir}' and SSH tunnel is disabled."
            )

        # 2. HTTP daemon resolution in chunks
        results: dict[str, ResolvedCommitContext] = {}
        url = f"http://127.0.0.1:{self._effective_port}/resolve_batch"

        for i in range(0, len(commits), self.batch_size):
            chunk = commits[i : i + self.batch_size]
            payload = json.dumps({"commits": [c.to_dict() for c in chunk]}).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=60.0) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        batch_res = data.get("results", {})
                        for sha, raw_ctx in batch_res.items():
                            results[sha] = ResolvedCommitContext.from_dict(raw_ctx)
                    else:
                        logger.warning("Mailing list server returned status %d", resp.status)
            except Exception as exc:
                logger.error("Failed to query mailing list daemon at %s: %s", url, exc)
                raise ConnectionError(f"Failed to query mailing list daemon at {url}: {exc}") from exc

        return results

    def get_email_detail(self, mid_or_num: str | int) -> dict[str, Any] | None:
        """Fetch full email detail by message ID or article number."""
        if self._in_process_resolver is not None:
            return self._in_process_resolver.store.get_email_detail(mid_or_num)

        url = f"http://127.0.0.1:{self._effective_port}/message"
        payload = json.dumps({"mid": str(mid_or_num)}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data.get("message")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            logger.warning("Mailing list server error for message %s: %s", mid_or_num, exc)
            raise ConnectionError(f"Mailing list daemon returned {exc.code}: {exc.reason}") from exc
        except Exception as exc:
            logger.error("Failed to fetch email %s from daemon: %s", mid_or_num, exc)
            raise ConnectionError(f"Failed to query mailing list daemon at {url}: {exc}") from exc
        return None

    def get_thread(self, tid: int) -> dict[str, Any]:
        """Fetch all messages in a thread by thread ID."""
        if self._in_process_resolver is not None:
            return self._in_process_resolver.store.get_thread_messages(tid)

        url = f"http://127.0.0.1:{self._effective_port}/thread?tid={tid}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.error("Failed to fetch thread %d from daemon: %s", tid, exc)
            return {"tid": tid, "subject": "", "total": 0, "participants": [], "messages": [], "error": str(exc)}
        return {"tid": tid, "subject": "", "total": 0, "participants": [], "messages": []}

    def search_emails(
        self,
        query: str = "",
        limit: int = 50,
        offset: int = 0,
        filter_type: str = "all",
    ) -> dict[str, Any]:
        """Search mailing list messages by query and filter."""
        if self._in_process_resolver is not None:
            return self._in_process_resolver.store.search_emails(
                query=query, limit=limit, offset=offset, filter_type=filter_type
            )

        url = f"http://127.0.0.1:{self._effective_port}/search"
        payload = json.dumps({
            "q": query,
            "limit": limit,
            "offset": offset,
            "filter": filter_type,
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.error("Failed to search mailing list at %s: %s", url, exc)
            return {"query": query, "filter_type": filter_type, "total": 0, "limit": limit, "offset": offset, "results": [], "error": str(exc)}
        return {"query": query, "filter_type": filter_type, "total": 0, "limit": limit, "offset": offset, "results": []}


    @staticmethod
    def _stage_row(target: Any, row: tuple[Any, ...]) -> None:
        from core.globalstuff import G

        table_id = getattr(target, "table_id", 48) if not isinstance(target, int) else target

        # If target is a Table schema instance (e.g. DBLayout.m_commit_email), stage into G.TE
        if hasattr(target, "table_name"):
            if hasattr(G, "TE") and G.TE is not None:
                if hasattr(G.TE, "start") and not getattr(G.TE, "tables", None):
                    G.TE.start([target], G.DB)
                G.TE.set(table_id, row)
            return

        # If target is a TableEngine / mock instance (e.g. MockTableEngine in unit tests)
        if hasattr(target, "set"):
            try:
                target.set(table_id, row)
                return
            except TypeError:
                try:
                    target.set(*row)
                    return
                except TypeError:
                    target.set(row)
                    return

        # Fallback to G.TE
        if hasattr(G, "TE") and G.TE is not None:
            if hasattr(G.TE, "start") and not getattr(G.TE, "tables", None):
                from core.DBLayout import m_commit_email
                G.TE.start([m_commit_email], G.DB)
            G.TE.set(table_id, row)


    @classmethod
    def stage_resolved_context(
        cls,
        m_commit_email_table: Any,
        commit_id: int,
        ctx: ResolvedCommitContext,
    ) -> list[tuple[Any, ...]]:
        """Stage rows into m_commit_email table for a resolved commit."""
        staged_rows: list[tuple[Any, ...]] = []

        # 1. Direct Patch email
        if ctx.patch_email and ctx.patch_email.mids:
            mid = ctx.patch_email.mids[0].strip("<> ")
            if mid:
                cls._stage_row(
                    m_commit_email_table,
                    (
                        commit_id,
                        mid,
                        ctx.patch_email.tid,
                        int(MLRelationType.Patch),
                        int(ctx.matched_tier),
                        int(ctx.patch_version),
                        int(ctx.patch_idx),
                        int(ctx.patch_total),
                    ),
                )
                staged_rows.append((
                    commit_id,
                    mid,
                    ctx.patch_email.tid,
                    int(MLRelationType.Patch),
                ))

        # 2. Series Cover Letter
        if ctx.cover_letter and ctx.cover_letter.mids:
            c_mid = ctx.cover_letter.mids[0].strip("<> ")
            if c_mid:
                cls._stage_row(
                    m_commit_email_table,
                    (
                        commit_id,
                        c_mid,
                        ctx.cover_letter.tid,
                        int(MLRelationType.CoverLetter),
                        int(ctx.matched_tier),
                        int(ctx.patch_version),
                        0,
                        int(ctx.patch_total),
                    ),
                )
                staged_rows.append((
                    commit_id,
                    c_mid,
                    ctx.cover_letter.tid,
                    int(MLRelationType.CoverLetter),
                ))

        # 3. Pull Request email (for merge commits)
        if ctx.pull_request and ctx.pull_request.mids:
            pr_mid = ctx.pull_request.mids[0].strip("<> ")
            if pr_mid:
                cls._stage_row(
                    m_commit_email_table,
                    (
                        commit_id,
                        pr_mid,
                        ctx.pull_request.tid,
                        int(MLRelationType.PullRequest),
                        int(MLMatchTier.PullRequest),
                        1,
                        0,
                        0,
                    ),
                )
                staged_rows.append((
                    commit_id,
                    pr_mid,
                    ctx.pull_request.tid,
                    int(MLRelationType.PullRequest),
                ))

        return staged_rows

    @classmethod
    def propagate_pull_request_to_children(
        cls,
        m_commit_email_table: Any,
        child_commit_ids: list[int],
        pr_mid: str,
        pr_tid: int,
    ) -> int:
        """Materialize Pull Request linkage across all child commits introduced by a merge."""
        clean_mid = pr_mid.strip("<> ")
        if not clean_mid or not child_commit_ids:
            return 0

        count = 0
        for cid in child_commit_ids:
            cls._stage_row(
                m_commit_email_table,
                (
                    cid,
                    clean_mid,
                    pr_tid,
                    int(MLRelationType.PullRequest),
                    int(MLMatchTier.PullRequest),
                    1,
                    0,
                    0,
                ),
            )
            count += 1
        return count

    # Alias for convenience
    resolve_batch = resolve_commits

    def close(self) -> None:
        """Close client and release underlying store resources and SSH tunnel."""
        if self._in_process_resolver is not None:
            if hasattr(self._in_process_resolver.store, "close"):
                self._in_process_resolver.store.close()
            self._in_process_resolver = None
        if self.use_tunnel:
            try:
                from core.ssh_tunnel import stop_ssh_tunnel
                stop_ssh_tunnel("mailing_list")
            except Exception as exc:
                logger.debug("Error stopping mailing_list SSH tunnel on client close: %s", exc)

