"""parser/mailing_list/ml_client.py - Client interface to Mailing List Resolver.

Handles direct in-process queries, SSH tunnel connection management, remote daemon
auto-launching, batch HTTP requests, and TableEngine staging into m_commit_email.
"""
from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
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


DAEMON_VERSION = "1.4.8"

# Total seconds to wait for a freshly launched remote daemon to report healthy
DAEMON_START_TIMEOUT = 30.0
# Startup attempts within that budget (a launch skipped by another starter's lock is retried)
DAEMON_START_ATTEMPTS = 2
# Seconds the remote startup script keeps the lock after launching, so a worker arriving
# right behind it cannot kill the daemon that was just started
LOCK_HOLD_SECONDS = 5
# Seconds a contending startup waits for that lock before giving up (must exceed the hold)
LOCK_WAIT_SECONDS = 8
# A running daemon of another version is replaced only once no client has used it for
# this long (no request in flight). A daemon in use, or one newer than this client, is
# reused as-is so one machine's start never kills the daemon another machine is querying.
DAEMON_IDLE_RESTART_SECONDS = 120
# Log the remote daemon writes; its mtime is the activity signal for daemons older than
# 1.4.8, whose /health does not report `idle_s`.
REMOTE_DAEMON_LOG = "/tmp/ml_server.log"


def _version_tuple(version: Any) -> tuple[int, ...]:
    """`"1.4.8"` -> `(1, 4, 8)`; an unparsable version sorts before every real one."""
    try:
        return tuple(int(part) for part in str(version).split("."))
    except (TypeError, ValueError):
        return ()


def daemon_reuse_reason(health: dict[str, Any] | None) -> str | None:
    """Why a responding daemon must be kept rather than replaced, or None if it may be replaced.

    Kept: the current version, a newer version, or an older one still in use (a request
    in flight or `idle_s` under `DAEMON_IDLE_RESTART_SECONDS`). Returns None for no daemon
    and for an outdated daemon that reports itself idle. A daemon without `idle_s` (older
    than 1.4.8) yields `"unknown"`: only the remote host can judge it (log mtime).
    """
    if not health:
        return None
    version = str(health.get("version") or "")
    if version == DAEMON_VERSION:
        return "current"
    if _version_tuple(version) > _version_tuple(DAEMON_VERSION):
        return "newer"
    if health.get("idle_s") is None:
        return "unknown"
    if int(health.get("in_flight") or 0) > 0 or float(health["idle_s"]) < DAEMON_IDLE_RESTART_SECONDS:
        return "in use"
    return None


def _build_remote_daemon_bundle(repo_root: Path) -> bytes:
    """Create a compressed tar bundle of `parser/mailing_list`, `db_engine`, and `core` `.py` modules."""
    buf = io.BytesIO()
    now = int(time.time())
    pkg_dirs = (
        Path("parser") / "mailing_list",
        Path("db_engine"),
        Path("core"),
    )
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel_dir in pkg_dirs:
            src_dir = repo_root / rel_dir
            if not src_dir.is_dir():
                continue
            for py_file in sorted(src_dir.glob("*.py")):
                if not py_file.is_file():
                    continue
                arcname = (rel_dir / py_file.name).as_posix()
                info = tar.gettarinfo(str(py_file), arcname=arcname)
                info.mtime = now
                with py_file.open("rb") as fh:
                    tar.addfile(info, fh)
    return buf.getvalue()


class MailingListClient:
    """Client for resolving kernel commits to mailing list emails."""

    DAEMON_START_TIMEOUT = DAEMON_START_TIMEOUT
    DAEMON_START_ATTEMPTS = DAEMON_START_ATTEMPTS
    LOCK_HOLD_SECONDS = LOCK_HOLD_SECONDS
    LOCK_WAIT_SECONDS = LOCK_WAIT_SECONDS
    DAEMON_IDLE_RESTART_SECONDS = DAEMON_IDLE_RESTART_SECONDS

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
        self.batch_size = max(1, int(self.cfg.get("batch_size", 50)))
        self.request_timeout = max(10.0, float(self.cfg.get("request_timeout", 180)))
        # Ceiling handed to the remote daemon, which shares its host with the archive
        # mirror and a database server.
        self.daemon_max_rss_mb = max(128, int(self.cfg.get("daemon_max_rss_mb", 1024)))
        self.daemon_max_concurrent = max(1, int(self.cfg.get("daemon_max_concurrent", 4)))
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

    def _probe_health(self, timeout: float = 2.0) -> dict[str, Any] | None:
        """GET /health on 127.0.0.1:<effective_port>; the decoded payload, or None if no daemon answers."""
        url = f"http://127.0.0.1:{self._effective_port}/health"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    if isinstance(data, dict) and data.get("status") == "ok":
                        return data
        except Exception:
            return None
        return None

    def _check_health(self, timeout: float = 2.0) -> bool:
        """True if a daemon (of any version) answers /health on 127.0.0.1:<effective_port>."""
        return self._probe_health(timeout) is not None

    def _ensure_remote_daemon_running(self) -> None:
        """Make sure a remote daemon answers, starting one only when none is usable.

        A responding daemon is reused when it runs this client's version, a newer one, or
        an older one other clients are still using (`daemon_reuse_reason`). Only when no
        daemon answers, or an outdated one has been idle for `DAEMON_IDLE_RESTART_SECONDS`,
        does a single SSH call ship the code bundle and (re)launch it; the remote side
        repeats that judgement under its lock, so a decision raced by another worker or by
        resumed traffic still never stops a daemon in use.
        """
        health = self._probe_health(timeout=1.5)
        reason = daemon_reuse_reason(health)
        if reason is not None and reason != "unknown":
            self._log_reused_daemon(health or {}, reason)
            return

        if health is None:
            logger.info("Remote mailing list daemon not reachable on port %d; attempting remote startup...", self._effective_port)
        else:
            logger.info(
                "Remote mailing list daemon on port %d runs version %s (client %s); replacing it if no client uses it...",
                self._effective_port, health.get("version"), DAEMON_VERSION,
            )
        host = self.tunnel_cfg.get("host", "")
        ssh_port = self.tunnel_cfg.get("port", 22)
        user = self.tunnel_cfg.get("user", "")
        key_file = self.tunnel_cfg.get("key_file", "")
        remote_port = self.tunnel_cfg.get("remote_port", 8192)

        if not host:
            raise ValueError("Cannot auto-start daemon: 'host' not configured in mailing_list.ssh_tunnel.")

        target = f"{user}@{host}" if user else host
        key_path = str(Path(os.path.expanduser(key_file)).resolve()) if key_file else None
        repo_root = Path(__file__).resolve().parents[2]
        bundle = _build_remote_daemon_bundle(repo_root)

        cmd = ["ssh", "-p", str(ssh_port), "-o", "BatchMode=yes"]
        if key_path:
            cmd.extend(["-i", key_path])
        cmd.extend([target, self._remote_start_script(int(remote_port))])
        ssh_timeout = self.LOCK_WAIT_SECONDS + self.LOCK_HOLD_SECONDS + 40

        # Launch and wait for health. A launch that another starter's lock skipped is
        # retried, so the client never gives up on a merely contended startup.
        attempt_wait = self.DAEMON_START_TIMEOUT / self.DAEMON_START_ATTEMPTS
        for attempt in range(1, self.DAEMON_START_ATTEMPTS + 1):
            outcome = ""
            try:
                res = subprocess.run(cmd, input=bundle, timeout=ssh_timeout, capture_output=True, check=False)
                out_lines = (res.stdout or b"").decode("utf-8", errors="replace").strip().splitlines()
                outcome = out_lines[-1].strip() if out_lines else ""
                if res.returncode != 0:
                    err = (res.stderr or b"").decode("utf-8", errors="replace").strip()
                    logger.warning("Remote daemon startup exited %d: %s", res.returncode, err or outcome or "no output")
            except Exception as exc:
                logger.warning("Remote daemon startup command failed: %s", exc)

            if outcome.startswith("KEEP"):
                kept = self._probe_health(timeout=5.0)
                if kept is not None:
                    self._log_reused_daemon(kept, outcome[4:].strip(" :") or "in use")
                    return

            start_t = time.time()
            while time.time() - start_t < attempt_wait:
                time.sleep(0.5)
                current = self._probe_health(timeout=2.0)
                if current is not None and (outcome.startswith("KEEP") or current.get("version") == DAEMON_VERSION):
                    logger.info("Remote mailing list daemon successfully initialized on port %d!", self._effective_port)
                    return
            if attempt < self.DAEMON_START_ATTEMPTS:
                logger.info(
                    "Remote daemon still not healthy after %.0fs; retrying startup (attempt %d/%d)...",
                    attempt_wait,
                    attempt + 1,
                    self.DAEMON_START_ATTEMPTS,
                )

        # An outdated daemon that still answers is better than none.
        fallback = self._probe_health(timeout=2.0)
        if fallback is not None:
            self._log_reused_daemon(fallback, "replacement did not come up")
            return

        # Fetch remote log tail if startup timed out
        remote_log = ""
        try:
            log_cmd = ["ssh", "-p", str(ssh_port), "-o", "BatchMode=yes"]
            if key_path:
                log_cmd.extend(["-i", key_path])
            log_cmd.extend([target, f"tail -n 10 {REMOTE_DAEMON_LOG} 2>/dev/null"])
            log_res = subprocess.run(log_cmd, timeout=5, capture_output=True, text=True, check=False)
            if log_res.stdout.strip():
                remote_log = f"\nRemote log ({REMOTE_DAEMON_LOG}):\n{log_res.stdout.strip()}"
        except Exception:
            pass

        raise ConnectionError(
            f"Remote mailing list daemon could not be reached on {target}:{remote_port} via port {self._effective_port}.{remote_log}"
        )

    def _log_reused_daemon(self, health: dict[str, Any], reason: str) -> None:
        version = health.get("version")
        if version == DAEMON_VERSION:
            logger.info("Mailing list daemon is healthy on port %d", self._effective_port)
        else:
            logger.warning(
                "Reusing mailing list daemon %s on port %d (client %s): %s; it is replaced once idle "
                "for %ds.",
                version, self._effective_port, DAEMON_VERSION, reason, self.DAEMON_IDLE_RESTART_SECONDS,
            )

    def _remote_start_script(self, remote_port: int) -> str:
        """Shell run on the archive host: keep a usable daemon, else unpack the bundle (stdin) and launch.

        - The advisory lock on fd 9 serializes concurrent startups from parser workers, and is
          held for LOCK_HOLD_SECONDS past the launch so a worker arriving a moment later finds
          the new daemon instead of starting another. A contending worker waits for the lock
          (LOCK_WAIT_SECONDS). The daemon is launched with `9>&-` so it does not inherit the
          lock fd and hold the lock for its whole lifetime, which would block every later start.
        - The probe inside the lock retries /health, so a momentarily busy daemon is never
          mistaken for a dead one, and keeps (prints `KEEP <reason>`, exit 0) a daemon that runs
          this or a newer version, or an older one with a request in flight or used within
          DAEMON_IDLE_RESTART_SECONDS (`idle_s`, else the daemon log's mtime). Only a missing,
          hung, or idle outdated daemon is replaced, and the code bundle is unpacked only then,
          so a running daemon never lazily imports a newer module.
        - Only the process owning the LISTENING socket is stopped (SIGTERM, then SIGKILL if it
          lingers). `fuser -k <port>/tcp` must not be used: it also kills every process merely
          *connected* to the port, including the sshd serving other workers' tunnels. A
          `pkill -f` on the module path is equally unusable, as it matches this shell's own
          command line. The listening socket is drained before binding so a killed predecessor
          cannot cause EADDRINUSE (the daemon also retries the bind).
        """
        probe = (
            "import json,os,sys,time,urllib.request\n"
            f"ours={_version_tuple(DAEMON_VERSION)!r}\n"
            "d=None\n"
            "for _ in range(3):\n"
            "    try:\n"
            f"        d=json.load(urllib.request.urlopen('http://127.0.0.1:{remote_port}/health',timeout=5)); break\n"
            "    except Exception:\n"
            "        time.sleep(1)\n"
            "if not isinstance(d,dict): sys.exit(1)\n"
            "v=str(d.get('version',''))\n"
            "try: vt=tuple(int(x) for x in v.split('.'))\n"
            "except ValueError: vt=()\n"
            "if vt==ours: print('KEEP current'); sys.exit(0)\n"
            "if vt>ours: print('KEEP newer version '+v); sys.exit(0)\n"
            "idle=d.get('idle_s')\n"
            "if idle is None:\n"
            f"    try: idle=time.time()-os.path.getmtime({REMOTE_DAEMON_LOG!r})\n"
            "    except OSError: idle=1e9\n"
            f"if int(d.get('in_flight') or 0)>0 or float(idle)<{self.DAEMON_IDLE_RESTART_SECONDS}:\n"
            "    print('KEEP in use (version %s, idle %.0fs)'%(v,float(idle))); sys.exit(0)\n"
            "sys.exit(1)\n"
        )
        lock_file = f"/tmp/ml_server.{remote_port}.lock"
        listen_pids = (
            f"ss -ltnpH sport = :{remote_port} 2>/dev/null "
            f"| grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u"
        )
        return (
            f"exec 9>{lock_file} || {{ cat >/dev/null; exit 1; }}; "
            f"flock -w {self.LOCK_WAIT_SECONDS} 9 || {{ cat >/dev/null; echo LOCKED; exit 0; }}; "
            f"if keep=$(printf '%s' {shlex.quote(probe)} | python3 - 2>/dev/null); then "
            f"cat >/dev/null; echo \"$keep\"; exit 0; fi; "
            f"mkdir -p ~/KernelInfo-Parser && "
            f"rm -rf ~/KernelInfo-Parser/parser/mailing_list/__pycache__ "
            f"~/KernelInfo-Parser/db_engine/__pycache__ ~/KernelInfo-Parser/core/__pycache__ && "
            f"tar -xzf - -C ~/KernelInfo-Parser || exit 1; "
            f"for p in $({listen_pids}); do kill $p >/dev/null 2>&1 || true; done; "
            f"for i in $(seq 1 20); do "
            f"[ -z \"$({listen_pids})\" ] && break; sleep 0.25; done; "
            f"for p in $({listen_pids}); do kill -9 $p >/dev/null 2>&1 || true; done; "
            f"for i in $(seq 1 20); do "
            f"[ -z \"$({listen_pids})\" ] && break; sleep 0.25; done; "
            f"cd ~/KernelInfo-Parser || exit 1; "
            f"nohup python3 -m parser.mailing_list.ml_server --storage-dir {shlex.quote(str(self.storage_dir))} "
            f"--port {remote_port} --max-rss-mb {self.daemon_max_rss_mb} "
            f"--max-concurrent {self.daemon_max_concurrent} "
            f">{REMOTE_DAEMON_LOG} 2>&1 </dev/null 9>&- & "
            f"sleep {self.LOCK_HOLD_SECONDS}; echo STARTED"
        )

    MAX_CHUNK_ATTEMPTS = 3
    # Pause before retrying a chunk the daemon refused for lack of memory or free slots.
    BACKPRESSURE_SLEEP = 2.0

    @staticmethod
    def _is_backpressure(exc: BaseException) -> bool:
        """True if the daemon refused the request because it is at its own limits."""
        return isinstance(exc, urllib.error.HTTPError) and exc.code in (503, 429)

    @staticmethod
    def _is_timeout(exc: BaseException) -> bool:
        """True if the exception chain is a socket read/connect timeout."""
        seen: set[int] = set()
        cur: BaseException | None = exc
        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))
            if isinstance(cur, TimeoutError):
                return True
            if isinstance(cur, urllib.error.URLError) and isinstance(cur.reason, BaseException):
                cur = cur.reason
                continue
            if "timed out" in str(cur).lower():
                return True
            cur = cur.__cause__ or cur.__context__
        return False

    def _post_resolve_chunk(
        self, url: str, chunk: list[CommitFeatures]
    ) -> tuple[dict[str, ResolvedCommitContext], list[str]]:
        """POST one chunk to /resolve_batch, returning (resolved, pending_shas).

        The daemon is given a time budget slightly below our socket timeout so it
        answers with partial results rather than letting the request time out.
        """
        budget_s = max(5.0, self.request_timeout * 0.8)
        payload = json.dumps({
            "commits": [c.to_dict() for c in chunk],
            "budget_s": budget_s,
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )

        resolved: dict[str, ResolvedCommitContext] = {}
        with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
            if resp.status != 200:
                logger.warning("Mailing list server returned status %d", resp.status)
                return resolved, []
            data = json.loads(resp.read().decode("utf-8"))

        for sha, raw_ctx in (data.get("results") or {}).items():
            resolved[sha] = ResolvedCommitContext.from_dict(raw_ctx)
        pending = [str(sha) for sha in (data.get("pending") or [])]
        return resolved, pending

    def resolve_commits(
        self, commits: list[CommitFeatures]
    ) -> dict[str, ResolvedCommitContext]:
        """Resolve a list of commit features against the mailing list archive.

        Remote resolution is adaptive: slow chunks are split in half and retried, and
        commits the daemon could not resolve within its budget are re-queued in smaller
        chunks. A chunk that keeps exceeding the budget is dropped with a warning rather
        than aborting the whole run, since mailing list linkage is best-effort
        enrichment. A genuinely unreachable daemon still raises ConnectionError.
        """
        if not commits:
            return {}

        # 1. In-process direct resolution
        if self._in_process_resolver is not None:
            return self._in_process_resolver.resolve_batch(commits)

        if not self.use_tunnel and not self._in_process_resolver:
            raise FileNotFoundError(
                f"No Linux kernel mailing list archive found at local path '{self.storage_dir}' and SSH tunnel is disabled."
            )

        # 2. HTTP daemon resolution in adaptive chunks
        results: dict[str, ResolvedCommitContext] = {}
        url = f"http://127.0.0.1:{self._effective_port}/resolve_batch"
        by_sha = {c.sha: c for c in commits}

        queue: list[tuple[list[CommitFeatures], int]] = [
            (commits[i : i + self.batch_size], 1)
            for i in range(0, len(commits), self.batch_size)
        ]
        dropped: list[str] = []
        recovered = False

        while queue:
            chunk, attempt = queue.pop(0)
            if not chunk:
                continue

            try:
                resolved, pending_shas = self._post_resolve_chunk(url, chunk)
            except Exception as exc:
                # The daemon protects its own memory ceiling; honour the backpressure
                # by shrinking the request rather than failing the run.
                if self._is_backpressure(exc):
                    time.sleep(self.BACKPRESSURE_SLEEP)
                    if len(chunk) > 1:
                        mid = len(chunk) // 2
                        queue[:0] = [(chunk[:mid], attempt), (chunk[mid:], attempt)]
                        logger.warning(
                            "Mailing list daemon is at its limits; retrying %d commits as halves of %d",
                            len(chunk),
                            mid,
                        )
                    elif attempt < self.MAX_CHUNK_ATTEMPTS:
                        queue.insert(0, (chunk, attempt + 1))
                    else:
                        dropped.append(chunk[0].sha)
                        logger.warning(
                            "Skipping commit %s: daemon stayed at its limits across %d attempts",
                            chunk[0].sha[:12],
                            attempt,
                        )
                    continue

                if isinstance(exc, urllib.error.HTTPError):
                    # HTTP errors from the daemon (e.g. 500 Internal Server Error, 400 Bad Request).
                    # Extract error body from the response so the real failure reason is logged.
                    try:
                        err_body = exc.read().decode("utf-8", errors="replace")
                    except Exception:
                        err_body = str(exc)
                    if len(chunk) > 1:
                        mid = len(chunk) // 2
                        queue[:0] = [(chunk[:mid], attempt), (chunk[mid:], attempt)]
                        logger.warning(
                            "Mailing list daemon returned HTTP %d for chunk of %d commits; retrying in halves of %d: %s",
                            exc.code,
                            len(chunk),
                            mid,
                            err_body,
                        )
                    else:
                        dropped.append(chunk[0].sha)
                        logger.warning(
                            "Skipping commit %s: daemon returned HTTP %d: %s",
                            chunk[0].sha[:12],
                            exc.code,
                            err_body,
                        )
                    continue

                if not self._is_timeout(exc):
                    # A dropped connection usually means the daemon was restarted (for
                    # example by another worker). Re-establish it once and retry.
                    if not recovered and self.auto_start:
                        recovered = True
                        logger.warning(
                            "Mailing list daemon connection lost (%s); re-establishing and retrying.",
                            exc,
                        )
                        try:
                            self._ensure_remote_daemon_running()
                        except Exception as rexc:
                            logger.error("Failed to query mailing list daemon at %s: %s", url, exc)
                            raise ConnectionError(f"Failed to query mailing list daemon at {url}: {exc}") from rexc
                        url = f"http://127.0.0.1:{self._effective_port}/resolve_batch"
                        queue.insert(0, (chunk, attempt))
                        continue
                    logger.error("Failed to query mailing list daemon at %s: %s", url, exc)
                    raise ConnectionError(f"Failed to query mailing list daemon at {url}: {exc}") from exc

                # Timed out: halve the chunk so slow commits get isolated.
                if len(chunk) > 1:
                    mid = len(chunk) // 2
                    queue[:0] = [(chunk[:mid], attempt), (chunk[mid:], attempt)]
                    logger.warning(
                        "Mailing list daemon timed out after %.0fs on %d commits; retrying in halves of %d",
                        self.request_timeout,
                        len(chunk),
                        mid,
                    )
                    continue

                # A single commit cannot be resolved within the timeout: verify the
                # daemon is still alive before writing it off.
                if not self._check_health(timeout=5.0):
                    logger.error("Failed to query mailing list daemon at %s: %s", url, exc)
                    raise ConnectionError(f"Failed to query mailing list daemon at {url}: {exc}") from exc
                dropped.append(chunk[0].sha)
                logger.warning(
                    "Skipping commit %s: mailing list resolution exceeded %.0fs",
                    chunk[0].sha[:12],
                    self.request_timeout,
                )
                continue

            results.update(resolved)

            # Re-queue commits the daemon deferred, in smaller chunks.
            deferred = [by_sha[sha] for sha in pending_shas if sha in by_sha]
            if not deferred:
                continue
            if attempt >= self.MAX_CHUNK_ATTEMPTS:
                dropped.extend(c.sha for c in deferred)
                logger.warning(
                    "Giving up on %d commits after %d mailing list resolution attempts",
                    len(deferred),
                    attempt,
                )
                continue
            sub_size = max(1, min(len(deferred), self.batch_size) // 2)
            queue[:0] = [
                (deferred[i : i + sub_size], attempt + 1)
                for i in range(0, len(deferred), sub_size)
            ]

        if dropped:
            logger.warning(
                "Mailing list resolution incomplete: %d/%d commits unresolved (daemon too slow).",
                len(dropped),
                len(commits),
            )

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

    def get_thread(self, tid: int, limit: int = 0, offset: int = 0) -> dict[str, Any]:
        """Fetch a page of a thread's messages by thread ID.

        The daemon bounds how many messages it unpacks (a thread can hold thousands),
        reporting the real count as `total` and whether more remain as `truncated`.
        """
        if self._in_process_resolver is not None:
            store = self._in_process_resolver.store
            if limit > 0:
                return store.get_thread_messages(tid, limit=limit, offset=offset)
            return store.get_thread_messages(tid, offset=offset)

        url = f"http://127.0.0.1:{self._effective_port}/thread?tid={tid}"
        if limit > 0:
            url += f"&limit={limit}"
        if offset > 0:
            url += f"&offset={offset}"
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

    def close(self, stop_tunnel: bool = False) -> None:
        """Close client and release underlying store resources and optionally SSH tunnel."""
        if self._in_process_resolver is not None:
            if hasattr(self._in_process_resolver.store, "close"):
                self._in_process_resolver.store.close()
            self._in_process_resolver = None
        if self.use_tunnel and stop_tunnel:
            try:
                from core.ssh_tunnel import stop_ssh_tunnel
                stop_ssh_tunnel("mailing_list")
            except Exception as exc:
                logger.debug("Error stopping mailing_list SSH tunnel on client close: %s", exc)

