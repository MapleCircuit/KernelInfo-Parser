"""core/distributed.py - Opt-In Multi-Computer Distributed Parsing Coordination.

Coordinates Master and Secondary (Worker) parser instances sharing a MariaDB database:
- Master parses mainline/master and populates delegable tasks (stable releases, past branches).
- Secondary Workers lease eligible tasks, parse them sequentially per chain, and update heartbeats.
- Heartbeat daemon renews leases and node registration.
- Range-based rollback and lease expiration recovery clean up crashed nodes without affecting peers.
"""
from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
import socket
import subprocess as sp
import threading
import time
from typing import Any

from core.DBLayout import TABLES, m_ingest_blob, m_ingest_state, m_parse_node, m_parse_task
from core.ingest_state import INGEST_DONE, INGEST_PARSING, rollback_version
from db_engine import DBError
from db_engine.spec import AnyOf, Case, Join, ge, gt, is_null, le, like, lt, ne, not_null
from parser.git_ast import release_order

logger = logging.getLogger(__name__)


# Roles
ROLE_STANDALONE = "standalone"
ROLE_MASTER = "master"
ROLE_WORKER = "worker"

# Task statuses in m_parse_task
TASK_PENDING = 0     # Waiting for base_commit_id to reach INGEST_DONE
TASK_ELIGIBLE = 1    # Base is INGEST_DONE; ready to be claimed by a secondary worker
TASK_LEASED = 2      # Claimed by a worker; lease is actively heartbeating
TASK_DONE = 3        # Ingest completed successfully
TASK_FAILED = 4      # Failed after maximum retries
TASK_RECLAIMING = 5  # Lease expired / reclaimed; rollback in progress before re-enqueue or failure

DEFAULT_LEASE_TTL = 180
DEFAULT_HEARTBEAT_INTERVAL = 30


def resolve_lease_settings(dist_cfg: Any) -> tuple[int, int]:
    """Return (lease_ttl, heartbeat_interval) in seconds from a `parser.distributed` config section.

    Reads `lease_ttl_seconds` and `heartbeat_seconds`; a missing or non-positive value takes its default. The
    lease is raised to at least twice the heartbeat interval so a live node always renews before it expires.
    """
    cfg = dist_cfg if isinstance(dist_cfg, dict) else {}

    def _positive(key: str, default: int) -> int:
        try:
            val = int(cfg.get(key, default))
        except (TypeError, ValueError):
            return default
        return val if val > 0 else default

    interval = _positive("heartbeat_seconds", DEFAULT_HEARTBEAT_INTERVAL)
    lease_ttl = max(_positive("lease_ttl_seconds", DEFAULT_LEASE_TTL), 2 * interval)
    return lease_ttl, interval


def get_default_node_id(role: str, worker_id: str | None = None) -> str:
    """Generate or sanitize unique identifier for this parser node (max 128 chars)."""
    hostname = socket.gethostname() or "node"
    pid = os.getpid()
    if worker_id and worker_id.strip():
        wid = worker_id.strip()
        parts = wid.split(":")
        tokens_to_add: list[str] = []
        if hostname not in parts:
            tokens_to_add.append(hostname)
        if str(pid) not in parts:
            tokens_to_add.append(str(pid))
        if tokens_to_add:
            full_id = f"{wid}:{':'.join(tokens_to_add)}"
        else:
            full_id = wid
    else:
        full_id = f"{role}:{hostname}:{pid}"

    if len(full_id) > 128:
        import hashlib
        h = hashlib.sha256(full_id.encode("utf-8")).hexdigest()[:16]
        full_id = f"{full_id[:111]}:{h}"
    return full_id


def ensure_distributed_tables(db: Any) -> None:
    """Create missing distributed coordination tables if not existing."""
    if db is None:
        return
    needed = [t for t in TABLES if t.table_name in ("m_table_sequence", "m_ingest_id_range", "m_parse_node", "m_parse_task")]
    missing = db.test_tables(needed)
    if missing:
        db.create_table([t for t in needed if t.table_name in missing])
    # Ensure m_ingest_state has base_commit_id and worker_id columns
    try:
        db.ensure_columns(m_ingest_state, ["base_commit_id", "worker_id"])
    except DBError as e:
        logger.warning(f"Column check for m_ingest_state: {e}")

    # Ensure index on m_ingest_blob(blob_id)
    try:
        if not db.index_exists("ingest_blob_blob_idx", m_ingest_blob):
            db.create_index("ingest_blob_blob_idx", m_ingest_blob, (m_ingest_blob.blob_id,))
    except DBError as e:
        logger.warning(f"Index check on m_ingest_blob(blob_id): {e}")


def _other_active_master(db: Any, network_id: int, node_id: str, now: int) -> tuple | None:
    """(node_id,) of another master node with a heartbeat in the last 120 seconds, or None."""
    return db.select_one(
        "m_parse_node",
        ["node_id"],
        {"network_id": network_id, "role": ROLE_MASTER, "heartbeat_at": ge(now - 120), "node_id": ne(node_id)},
        as_dict=False,
    )


def register_node(db: Any, node_id: str, role: str, network_id: int = 1) -> None:
    """Register parser node in m_parse_node."""
    if db is None:
        return
    hostname = socket.gethostname() or ""
    pid = os.getpid()
    now = int(time.time())
    if role == ROLE_MASTER:
        row = _other_active_master(db, network_id, node_id, now)
        if row:
            other_id = row[0]
            raise RuntimeError(f"Another master node '{other_id}' is already active for network {network_id}.")
    db.insert_rows(
        "m_parse_node",
        ["node_id", "network_id", "role", "hostname", "pid", "active_commit_id", "heartbeat_at"],
        [(node_id, network_id, role, hostname, pid, 0, now)],
        on_conflict="update",
        update_columns=["network_id", "role", "hostname", "pid", "active_commit_id", "heartbeat_at"],
    )
    if role == ROLE_MASTER:
        # Re-check after insertion to prevent concurrent master registration race
        row = _other_active_master(db, network_id, node_id, now)
        if row:
            other_id = row[0]
            if other_id < node_id:
                db.delete_rows("m_parse_node", {"node_id": node_id})
                raise RuntimeError(f"Another master node '{other_id}' is already active for network {network_id}.")
    logger.info(f"[Distributed] Registered {role} parser node '{node_id}' (PID {pid}).")


def unregister_node(db: Any, node_id: str) -> None:
    """Remove parser node from m_parse_node on shutdown."""
    if db is None:
        return
    try:
        db.delete_rows("m_parse_node", {"node_id": node_id})
    except Exception:  # best effort at shutdown (the database may already be unreachable)
        pass
    logger.info(f"[Distributed] Unregistered parser node '{node_id}'.")


def update_node_heartbeat(
    db: Any,
    node_id: str,
    active_commit_id: int = 0,
    current_task_id: int | None = None,
    lease_ttl: int = DEFAULT_LEASE_TTL,
    role: str = "worker",
    network_id: int = 1,
    allow_insert: bool = True,
) -> None:
    """Update heartbeat timestamp and renew active task lease."""
    if db is None:
        return
    now = int(time.time())
    try:
        with db.transaction():
            rc = db.update_rows("m_parse_node", {"heartbeat_at": now, "active_commit_id": active_commit_id}, {"node_id": node_id})
            if rc == 0 and allow_insert:
                db.insert_rows(
                    "m_parse_node",
                    ["node_id", "hostname", "pid", "active_commit_id", "heartbeat_at", "network_id", "role"],
                    [(node_id, socket.gethostname() or "", os.getpid(), active_commit_id, now, network_id, role)],
                    on_conflict="update",
                    update_columns=["heartbeat_at", "active_commit_id"],
                )
            if current_task_id is not None:
                db.update_rows(
                    "m_parse_task",
                    {"lease_expires_at": now + lease_ttl},
                    {"task_id": current_task_id, "worker_id": node_id, "status": TASK_LEASED},
                )
            elif active_commit_id > 0:
                db.update_rows(
                    "m_parse_task",
                    {"lease_expires_at": now + lease_ttl},
                    {"commit_id": active_commit_id, "worker_id": node_id, "status": TASK_LEASED},
                )
    except Exception as e:
        logger.warning(f"[Distributed] Heartbeat update error for node '{node_id}': {e}")


class NodeHeartbeatDaemon:
    """Background daemon thread updating node heartbeat and task lease periodically."""

    def __init__(
        self,
        db_factory: Any,
        node_id: str,
        interval: int = DEFAULT_HEARTBEAT_INTERVAL,
        lease_ttl: int = DEFAULT_LEASE_TTL,
        role: str = "worker",
        network_id: int = 1,
    ) -> None:
        self.db_factory = db_factory
        self.node_id = node_id
        self.interval = interval
        self.lease_ttl = lease_ttl
        self.role = role
        self.network_id = network_id
        self.active_commit_id: int = 0
        self.current_task_id: int | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start heartbeat thread (idempotent)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, name=f"Heartbeat-{self.node_id}", daemon=True)
            self._thread.start()

    def set_active(self, commit_id: int, task_id: int | None = None) -> None:
        """Set active commit and task currently being parsed."""
        with self._lock:
            self.active_commit_id = commit_id
            if task_id is not None or self.current_task_id is None:
                self.current_task_id = task_id

    def clear_active(self) -> None:
        """Clear active commit/task when version completes."""
        with self._lock:
            self.active_commit_id = 0
            self.current_task_id = None

    def stop(self) -> None:
        """Stop heartbeat thread."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        last_warn_time = 0.0
        while not self._stop_event.wait(self.interval):
            if self._stop_event.is_set():
                break
            with self._lock:
                cid = self.active_commit_id
                tid = self.current_task_id
            try:
                with self.db_factory() as db:
                    if self._stop_event.is_set():
                        break
                    update_node_heartbeat(
                        db,
                        self.node_id,
                        active_commit_id=cid,
                        current_task_id=tid,
                        lease_ttl=self.lease_ttl,
                        role=self.role,
                        network_id=self.network_id,
                        allow_insert=not self._stop_event.is_set(),
                    )
            except Exception as e:
                now = time.time()
                if now - last_warn_time > 60:
                    logger.warning(f"[Distributed] Daemon heartbeat iteration failed: {e}")
                    last_warn_time = now
                else:
                    logger.debug(f"[Distributed] Daemon heartbeat iteration failed: {e}")


def unlock_dependent_tasks(
    db: Any,
    network_id: int,
    base_commit_id: int,
    base_ref_name: str | None = None,
) -> int:
    """Unlock tasks waiting on base_commit_id from PENDING to ELIGIBLE."""
    if db is None or (not base_commit_id and not base_ref_name):
        return 0
    if base_ref_name is None and base_commit_id > 0:
        found = db.select_one(
            ("m_git_ref", "r"),
            ["r.ref_name"],
            {"repo.network_id": network_id, "r.commit_id": base_commit_id},
            joins=[Join(("m_repository", "repo"), {"repo.repo_id": "r.repo_id"})],
            order_by=["r.ref_type"],
            as_dict=False,
        )
        if found and found[0]:
            base_ref_name = str(found[0])

    pending = {"network_id": network_id, "status": TASK_PENDING}
    count = 0
    if base_ref_name and base_commit_id > 0:
        count = db.update_rows(
            "m_parse_task",
            {"status": TASK_ELIGIBLE, "base_commit_id": base_commit_id},
            [pending, AnyOf({"base_commit_id": base_commit_id}, {"base_ref_name": base_ref_name})],
        ) or 0
    elif base_ref_name:
        count = db.update_rows("m_parse_task", {"status": TASK_ELIGIBLE}, {**pending, "base_ref_name": base_ref_name}) or 0
    elif base_commit_id > 0:
        count = db.update_rows("m_parse_task", {"status": TASK_ELIGIBLE}, {**pending, "base_commit_id": base_commit_id}) or 0
    if count:
        ref_info = f" (ref '{base_ref_name}')" if base_ref_name else ""
        logger.info(
            f"[Distributed] Unlocked {count} task(s) dependent on completed base commit {base_commit_id}{ref_info} to ELIGIBLE."
        )
    return count


def resolve_branch_base_dependency(
    repo_dir: str | Path,
    branch: str,
    db: Any = None,
    network_id: int = 1,
    version_states: dict[str, tuple[int, str]] | None = None,
) -> tuple[str, int]:
    """Determine upstream base ref and commit ID for a branch using git merge-base."""
    upstream = "history/master" if str(branch).startswith("history/") else "master"
    try:
        res = sp.run(
            ["git", "-C", str(repo_dir), "merge-base", str(branch), upstream],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return ("", 0)
        sha = res.stdout.strip()
        tag_res = sp.run(
            ["git", "-C", str(repo_dir), "tag", "--points-at", sha],
            capture_output=True,
            text=True,
            check=False,
        )
        tags = [t.strip() for t in tag_res.stdout.splitlines() if t.strip()]
        base_ref = tags[0] if tags else upstream

        base_cid = 0
        if version_states and base_ref in version_states:
            base_cid = version_states[base_ref][0]
        elif db is not None and len(sha) == 40:
            row = db.select_one(
                "m_commit", ["commit_id"], {"network_id": network_id, "commit_hash": bytes.fromhex(sha)}, as_dict=False
            )
            if row and row[0]:
                base_cid = int(row[0])
        return (base_ref, base_cid)
    except Exception as e:
        logger.warning("Could not resolve merge-base for branch '%s': %s", branch, e)
        return ("", 0)


def publish_delegable_chains(
    db: Any,
    network_id: int,
    repo_id: int,
    all_tags: list[str],
    version_states: dict[str, tuple[int, str]],
    delegate_stable: bool = True,
    delegate_branches: list[str] | None = None,
    repo_dir: str | Path | None = None,
) -> int:
    """Analyze tags using release_order, partition into chains, and publish delegable tasks to m_parse_task.

    Args:
        db: Active database driver handle.
        network_id: Project git network id.
        repo_id: Repository id.
        all_tags: Sorted list of all canonical tags in the repo.
        version_states: ref_name -> (commit_id, state) from fetch_repo_version_states.
        delegate_stable: If True, stable release series (vX.Y.1 .. vX.Y.Z) are published for secondary parsers.
        delegate_branches: Optional list of specific branches or past tag ranges to delegate.
        repo_dir: Optional repository path for branch merge-base discovery.

    Returns:
        Number of delegable tasks enqueued or updated.
    """
    if db is None:
        return 0

    ranges = release_order.release_ranges(all_tags)
    tasks_to_insert: list[tuple[int, str, str, int, str, int, str, int, int]] = []
    # (network_id, chain_name, chain_kind, seq_order, ref_name, commit_id, base_ref_name, base_commit_id, status)

    done_commit_ids: set[int] = {
        cid for rname, (cid, st) in version_states.items() if st == "done"
    }

    # Group stable and next ranges by series
    for r in ranges:
        tag = r["tag"]
        kind = r["kind"]
        if kind == "mainline":
            # Mainline is parsed by Master; not delegated
            continue

        if kind == "stable" and not delegate_stable:
            continue

        cid, st = version_states.get(tag, (0, "new"))
        base_tag = r["segment_base"] or ""
        base_cid, base_st = version_states.get(base_tag, (0, "new")) if base_tag else (0, "new")

        # Determine chain_name
        p = release_order.parse_tag(tag)
        if kind == "next":
            series_str = release_order.series_name(p["series"]) if p else "next"
            chain_name = f"next:{series_str}"
            seq_order = p["stable"] if p else 0
        else:
            series_str = release_order.series_name(p["series"]) if p else "misc"
            chain_name = f"stable:{series_str}"
            seq_order = p["stable"] if p else 0

        initial_status = TASK_PENDING
        if st == "done":
            initial_status = TASK_DONE
        elif not base_tag or (base_cid in done_commit_ids and base_cid > 0) or version_states.get(base_tag, (0, ""))[1] == "done":
            initial_status = TASK_ELIGIBLE

        tasks_to_insert.append((
            network_id,
            chain_name,
            kind,
            seq_order,
            tag,
            cid,
            base_tag,
            base_cid,
            initial_status,
        ))

    # Add custom or auto-discovered delegated branches
    branches_to_delegate = list(delegate_branches) if delegate_branches is not None else []
    if delegate_branches is None and repo_dir:
        from core.config import find_repository_branches
        try:
            discovered = find_repository_branches(Path(repo_dir))
            trunks = {"history/master", "history/HEAD", "master", "main", "origin/master", "origin/main"}
            branches_to_delegate = [b for b in discovered if b not in trunks]
        except Exception as e:
            logger.warning("Could not auto-discover branches for delegation: %s", e)

    repo_path = repo_dir or "repos/linux"
    for b_idx, b_ref in enumerate(branches_to_delegate):
        cid, st = version_states.get(b_ref, (0, "new"))
        b_kind = "history" if str(b_ref).startswith("history/") else ("next" if "next" in str(b_ref) else "branch")
        chain_name = f"branch:{b_ref}"
        base_ref, base_cid = resolve_branch_base_dependency(
            repo_path, b_ref, db=db, network_id=network_id, version_states=version_states
        )
        initial_status = TASK_PENDING
        if st == "done":
            initial_status = TASK_DONE
        elif not base_ref or (base_cid in done_commit_ids and base_cid > 0) or version_states.get(base_ref, (0, ""))[1] == "done":
            initial_status = TASK_ELIGIBLE
        tasks_to_insert.append((
            network_id,
            chain_name,
            b_kind,
            b_idx,
            b_ref,
            cid,
            base_ref,
            base_cid,
            initial_status,
        ))

    if not tasks_to_insert:
        return 0

    enqueued = db.enqueue_parse_tasks(tasks_to_insert)

    logger.info(f"[Distributed] Published {len(tasks_to_insert)} task(s) into m_parse_task ({enqueued} new/updated).")
    return enqueued


# Eligible tasks read per claim scan: one ready head per chain whose base is parsed, far fewer in practice
_CLAIM_SCAN_LIMIT = 2000


_CLAIM_COLUMNS = ["task_id", "chain_name", "chain_kind", "ref_name", "commit_id", "base_ref_name", "base_commit_id"]
# Database-side order of the claim scan (the candidates are then sorted with `_claim_priority()` and `release_key`)
_CLAIM_ORDER = Case(
    [
        (AnyOf({"chain_name": like("history")}, {"chain_kind": "history"}), 1),
        ({"chain_kind": "stable"}, 2),
        (AnyOf({"chain_name": like("next", mode="contains")}, {"chain_kind": "next"}), 3),
    ],
    4,
)


def _claim_priority(chain_name: str, chain_kind: str) -> int:
    """Chain preference of `claim_next_task()`: history branches, stable series, linux-next, other branches."""
    if chain_name.startswith("history") or chain_kind == "history":
        return 1
    if chain_kind == "stable":
        return 2
    if "next" in chain_name or chain_kind == "next":
        return 3
    return 4


def claim_next_task(
    db: Any,
    network_id: int,
    worker_id: str,
    preferred_chain: str | None = None,
    lease_ttl: int = DEFAULT_LEASE_TTL,
) -> dict[str, Any] | None:
    """Claim an ELIGIBLE task from m_parse_task, prioritizing preferred_chain for cache locality.

    Returns task dictionary or None if no eligible task is currently available.
    """
    if db is None:
        return None

    now = int(time.time())
    expires_at = now + lease_ttl

    # 1. Try preferred_chain first if specified
    candidates: list[tuple[int, str, str, str, int, str, int]] = []
    if preferred_chain:
        rows = db.select_rows(
            "m_parse_task",
            _CLAIM_COLUMNS,
            {"network_id": network_id, "status": TASK_ELIGIBLE, "chain_name": preferred_chain},
            order_by=["seq_order"],
            limit=5,
            as_dict=False,
        )
        candidates = [
            (int(r[0]), str(r[1]), str(r[2]), str(r[3]), int(r[4]), str(r[5]), int(r[6]))
            for r in rows
        ]

    # 2. If no candidate in preferred chain, scan across all eligible tasks, oldest release first
    if not candidates:
        rows = db.select_rows(
            "m_parse_task",
            _CLAIM_COLUMNS,
            {"network_id": network_id, "status": TASK_ELIGIBLE},
            order_by=[_CLAIM_ORDER, "chain_name", "seq_order"],
            limit=_CLAIM_SCAN_LIMIT,
            as_dict=False,
        )
        candidates = [
            (int(r[0]), str(r[1]), str(r[2]), str(r[3]), int(r[4]), str(r[5]), int(r[6]))
            for r in rows
        ]
        # SQL orders chain names as text (stable:v3.0 < stable:v3.10 < stable:v2.6.11 by length or bytes)
        candidates.sort(key=lambda c: (_claim_priority(c[1], c[2]), release_order.release_key(c[3])))
        candidates = candidates[:10]

    for cand in candidates:
        t_id, c_name, c_kind, r_name, cid, b_name, b_cid = cand
        # Atomic claim: only one worker changes the row from ELIGIBLE (the affected-row count tells who won)
        rc = db.update_rows(
            "m_parse_task",
            {"status": TASK_LEASED, "worker_id": worker_id, "lease_expires_at": expires_at},
            {"task_id": t_id, "status": TASK_ELIGIBLE},
        )
        if rc and rc > 0:
            fresh_row = db.select_one(
                "m_parse_task", ["commit_id", "base_commit_id", "base_ref_name"], {"task_id": t_id}, as_dict=False
            )
            if fresh_row:
                cid = int(fresh_row[0] or 0)
                b_cid = int(fresh_row[1] or 0)
                b_name = str(fresh_row[2] or "")
            logger.info(
                f"[Distributed] Worker '{worker_id}' successfully leased task {t_id}: "
                f"'{r_name}' (chain '{c_name}', base '{b_name}', commit_id {cid})."
            )
            return {
                "task_id": t_id,
                "chain_name": c_name,
                "chain_kind": c_kind,
                "ref_name": r_name,
                "commit_id": cid,
                "base_ref_name": b_name,
                "base_commit_id": b_cid,
            }

    return None


def check_lease_held(db: Any, task_id: int, worker_id: str) -> bool:
    """Check if worker_id currently holds an active unexpired lease on task_id."""
    if db is None:
        return False
    now = int(time.time())
    return db.row_exists(
        "m_parse_task",
        {"task_id": task_id, "worker_id": worker_id, "status": TASK_LEASED, "lease_expires_at": gt(now)},
    )


def mark_task_done(
    db: Any,
    task_id: int,
    commit_id: int,
    network_id: int,
    ref_name: str | None = None,
    worker_id: str | None = None,
) -> bool:
    """Mark task as DONE and unlock dependent tasks."""
    if db is None:
        return False
    if ref_name is None:
        row = db.select_one("m_parse_task", ["ref_name"], {"task_id": task_id}, as_dict=False)
        if row and row[0]:
            ref_name = str(row[0])

    where: dict[str, Any] = {"task_id": task_id, "status": TASK_LEASED}
    if worker_id:
        where["worker_id"] = worker_id
    rc = db.update_rows("m_parse_task", {"status": TASK_DONE, "commit_id": commit_id, "error_msg": ""}, where)
    if not (rc and rc > 0):
        logger.warning(
            f"[Distributed] Task {task_id} lease was lost/expired; skipping task completion."
        )
        return False

    unlock_dependent_tasks(db, network_id, commit_id, base_ref_name=ref_name)
    return True


def _dependents_where(network_id: int, cids: set[int], refs: set[str], statuses: list[int]) -> list[Any]:
    """Where of the tasks of `statuses` whose base is one of the commits or refs."""
    branches: list[Any] = []
    if cids:
        branches.append({"base_commit_id": sorted(cids)})
    if refs:
        branches.append({"base_ref_name": sorted(refs)})
    return [{"network_id": network_id, "status": statuses}, AnyOf(*branches)]


def _cascade_task_failure(db: Any, initial_task_id: int) -> int:
    """Cascade failure to all downstream dependent tasks until fixed-point."""
    total_cascaded = 0
    row = db.select_one("m_parse_task", ["network_id", "commit_id", "ref_name"], {"task_id": initial_task_id}, as_dict=False)
    if not row:
        return 0
    net_id = int(row[0]) if row[0] is not None else 0
    init_cid = int(row[1]) if row[1] and int(row[1]) > 0 else 0
    init_rname = str(row[2]) if row[2] else ""

    curr_cids = {init_cid} if init_cid > 0 else set()
    curr_refs = {init_rname} if init_rname else set()
    seen_cids: set[int] = set()
    seen_refs: set[str] = set()
    waiting = [TASK_PENDING, TASK_ELIGIBLE]

    with db.transaction():
        while curr_cids or curr_refs:
            step_cids = curr_cids - seen_cids
            step_refs = curr_refs - seen_refs
            seen_cids.update(step_cids)
            seen_refs.update(step_refs)

            if not step_cids and not step_refs:
                break

            affected = db.update_rows(
                "m_parse_task",
                {"status": TASK_FAILED, "error_msg": "Prerequisite base task failed"},
                _dependents_where(net_id, step_cids, step_refs, waiting),
            ) or 0
            total_cascaded += affected
            if affected == 0:
                break

            next_cids: set[int] = set()
            next_refs: set[str] = set()
            rows = db.select_rows("m_parse_task", ["commit_id", "ref_name"], {"network_id": net_id, "status": TASK_FAILED}, as_dict=False)
            for f_cid, f_rname in rows:
                fc = int(f_cid) if f_cid and int(f_cid) > 0 else 0
                fr = str(f_rname) if f_rname else ""
                if fc > 0 and fc not in seen_cids:
                    next_cids.add(fc)
                if fr and fr not in seen_refs:
                    next_refs.add(fr)

            if not next_cids and not next_refs:
                break
            if not db.row_exists("m_parse_task", _dependents_where(net_id, next_cids, next_refs, waiting)):
                break

            curr_cids = next_cids
            curr_refs = next_refs

    return total_cascaded


_MAX_ATTEMPTS = 3  # attempts before a task is FAILED


def mark_task_failed(
    db: Any,
    task_id: int,
    error_msg: str,
    worker_id: str | None = None,
) -> bool:
    """Record task failure atomically; retry up to 3 times before setting TASK_FAILED.

    Returns True if the task was updated, False if lease was already revoked or not held.
    """
    if db is None:
        return False

    rc = db.record_task_failure(task_id, error_msg, worker_id=worker_id, max_attempts=_MAX_ATTEMPTS)
    if not (rc and rc > 0):
        return False

    row = db.select_one("m_parse_task", ["status", "retry_count"], {"task_id": task_id}, as_dict=False)
    new_status = int(row[0]) if row else TASK_FAILED
    new_retries = int(row[1]) if row else _MAX_ATTEMPTS

    logger.warning(
        f"[Distributed] Task {task_id} failed (attempt {new_retries}/{_MAX_ATTEMPTS}, status {new_status}): {error_msg}"
    )

    if new_status == TASK_FAILED:
        _cascade_task_failure(db, task_id)

    return True


def release_task(db: Any, task_id: int, reason: str, worker_id: str) -> bool:
    """Hand a task leased by `worker_id` back to the queue as ELIGIBLE without counting an attempt.

    For failures of the node rather than of the task (a broken parser environment): another node parses it.
    Returns True if the task was released, False if `worker_id` no longer held its lease.
    """
    if db is None:
        return False
    released = bool(db.update_rows(
        "m_parse_task",
        {"status": TASK_ELIGIBLE, "worker_id": "", "lease_expires_at": 0, "error_msg": reason[:1000]},
        {"task_id": task_id, "status": [TASK_LEASED, TASK_RECLAIMING], "worker_id": worker_id},
    ))
    if released:
        logger.warning(f"[Distributed] Task {task_id} released back to the queue by '{worker_id}': {reason}")
    return released


def reclaim_expired_leases(db: Any, network_id: int, lease_ttl: int = DEFAULT_LEASE_TTL) -> int:
    """Find expired leases where the owning worker died, roll back their partial rows, and re-enqueue."""
    if db is None:
        return 0

    now = int(time.time())
    reclaimed = 0

    try:
        db.delete_rows("m_parse_node", {"heartbeat_at": lt(now - 86400)})
    except DBError as e:
        logger.debug(f"[Distributed] Dead node cleanup error: {e}")

    # Find tasks in TASK_LEASED whose lease expired or whose owning node is dead.
    # Also include tasks in TASK_RECLAIMING whose 300s reclaim lease has expired.
    # Tasks in TASK_RECLAIMING whose lease has NOT expired are excluded!
    dead_heartbeat_threshold = now - (lease_ttl + 30)
    node_gone = AnyOf({"n.node_id": is_null()}, [{"n.heartbeat_at": not_null()}, {"n.heartbeat_at": lt(dead_heartbeat_threshold)}])
    rows = db.select_rows(
        ("m_parse_task", "t"),
        ["t.task_id", "t.commit_id", "t.worker_id", "t.ref_name", Case([([node_gone], 1)], 0), "t.status"],
        [
            {"t.network_id": network_id},
            AnyOf(
                [{"t.status": TASK_LEASED}, AnyOf({"t.lease_expires_at": le(now)}, node_gone)],
                {"t.status": TASK_RECLAIMING, "t.lease_expires_at": le(now)},
            ),
        ],
        joins=[Join(("m_parse_node", "n"), {"n.node_id": "t.worker_id"}, "left")],
        as_dict=False,
    )
    expired = []
    for r in rows:
        if len(r) >= 6:
            expired.append((int(r[0]), int(r[1]), str(r[2]), str(r[3]), int(r[4]), int(r[5])))
        elif len(r) >= 4:
            expired.append((int(r[0]), int(r[1]), str(r[2]), str(r[3]), 0, TASK_LEASED))

    for t_id, cid, w_id, r_name, node_dead, prev_status in expired:
        claim: dict[str, Any] = {"task_id": t_id, "status": prev_status, "worker_id": w_id}
        if not node_dead:
            claim["lease_expires_at"] = le(now)  # a dead owner's task is taken whatever its lease says
        rc = db.update_rows(
            "m_parse_task",
            {"status": TASK_RECLAIMING, "lease_expires_at": now + 300, "worker_id": "reclaiming",
             "error_msg": "Reclaiming expired lease"},
            claim,
        )
        if not (rc and rc > 0):
            continue

        logger.warning(
            f"[Distributed] Reclaiming expired lease on task {t_id} ('{r_name}', worker '{w_id}', commit_id {cid})."
        )
        if cid == 0 and r_name:
            found = db.select_one(
                ("m_git_ref", "r"),
                ["r.commit_id"],
                {"repo.network_id": network_id, "r.ref_name": r_name, "r.commit_id": gt(0)},
                joins=[Join(("m_repository", "repo"), {"repo.repo_id": "r.repo_id"})],
                as_dict=False,
            )
            if found and found[0]:
                cid = int(found[0])

        # Check if version was already marked INGEST_DONE before node crashed/lease expired (P1 #14)
        is_already_done = False
        if cid > 0:
            st_row = db.select_one("m_ingest_state", ["status"], {"network_id": network_id, "commit_id": cid}, as_dict=False)
            if st_row and st_row[0] == INGEST_DONE:
                is_already_done = True

        if is_already_done:
            # Worker completed parsing before crash/timeout; do not rollback! Mark DONE.
            db.update_rows(
                "m_parse_task",
                {"status": TASK_DONE, "commit_id": cid, "error_msg": "", "worker_id": ""},
                {"task_id": t_id, "status": TASK_RECLAIMING},
            )
            unlock_dependent_tasks(db, network_id, cid, base_ref_name=r_name)
            reclaimed += 1
            continue

        rollback_ok = True
        if cid > 0:
            try:
                rb_result = rollback_version(
                    db, network_id, cid, TABLES
                )
                if rb_result is False:
                    logger.error(f"[Distributed] Rollback returned False for expired task {t_id} (commit {cid}).")
                    rollback_ok = False
            except Exception as e:
                logger.error(f"[Distributed] Rollback error for expired task {t_id}: {e}", exc_info=True)
                rollback_ok = False

        if not rollback_ok:
            # P0 #6: Failed or skipped rollback - mark task as TASK_FAILED directly with clear error
            db.update_rows(
                "m_parse_task",
                {"status": TASK_FAILED, "error_msg": f"Rollback failed during reclaim: commit {cid}", "worker_id": ""},
                {"task_id": t_id, "status": TASK_RECLAIMING},
            )
            _cascade_task_failure(db, t_id)
        else:
            mark_task_failed(db, t_id, f"Worker lease expired ({w_id})", worker_id="reclaiming")
        reclaimed += 1

    return reclaimed


def is_parsing_finished(db: Any, network_id: int, master_heartbeat_timeout: int = 120) -> bool:
    """Check whether all parsing across the repository has completed.

    Used by Secondary Workers to decide whether to wait for more versions or exit cleanly.
    Returns True ONLY when:
    1. Tasks have been published (total_tasks > 0).
    2. No runnable tasks remain (ELIGIBLE, LEASED, or RECLAIMING is 0).
    3. No Master parser node is actively heartbeating.
    4. All tasks are completed, or any remaining PENDING tasks cannot be unblocked.
    """
    if db is None:
        return False

    now = int(time.time())

    # 1. Total tasks check: if no tasks published yet, worker must wait for Master
    total_tasks = db.count_rows("m_parse_task", {"network_id": network_id})
    if total_tasks == 0:
        return False

    # 2. Check runnable tasks (ELIGIBLE, LEASED, or RECLAIMING)
    runnable_tasks = db.count_rows("m_parse_task", {"network_id": network_id, "status": [TASK_ELIGIBLE, TASK_LEASED, TASK_RECLAIMING]})
    if runnable_tasks > 0:
        return False

    # 3. Check if Master is still active
    active_master = db.count_rows(
        "m_parse_node", {"network_id": network_id, "role": ROLE_MASTER, "heartbeat_at": ge(now - master_heartbeat_timeout)}
    )
    if active_master > 0:
        return False

    # 4. If active_master == 0 and runnable_tasks == 0:
    # Check remaining PENDING tasks
    pending_tasks = db.count_rows("m_parse_task", {"network_id": network_id, "status": TASK_PENDING})
    if pending_tasks == 0:
        return True

    # Check all remaining PENDING tasks
    pending_rows = db.select_rows(
        "m_parse_task", ["task_id", "base_ref_name", "base_commit_id"], {"network_id": network_id, "status": TASK_PENDING},
        as_dict=False,
    )
    for prow in pending_rows:
        b_ref = str(prow[1]) if prow[1] else ""
        b_cid = int(prow[2]) if prow[2] else 0

        # Query base task in m_parse_task
        base_matches: list[Any] = [{"ref_name": b_ref}]
        if b_cid > 0:
            base_matches.append({"commit_id": b_cid})
        base_row = db.select_one("m_parse_task", ["status"], [{"network_id": network_id}, AnyOf(*base_matches)], as_dict=False)

        # Query base state in m_ingest_state
        ingest_row = (
            db.select_one("m_ingest_state", ["status"], {"network_id": network_id, "commit_id": b_cid}, as_dict=False)
            if b_cid > 0 else None
        )

        # Check if base is in progress
        base_task_in_progress = base_row is not None and base_row[0] in (TASK_PENDING, TASK_ELIGIBLE, TASK_LEASED, TASK_RECLAIMING)
        base_ingest_in_progress = ingest_row is not None and ingest_row[0] == INGEST_PARSING
        if base_task_in_progress or base_ingest_in_progress:
            return False

        # Check if base is completed (missed unlock)
        base_task_done = base_row is not None and base_row[0] == TASK_DONE
        base_ingest_done = ingest_row is not None and ingest_row[0] == INGEST_DONE
        if base_task_done or base_ingest_done:
            unlock_dependent_tasks(db, network_id, b_cid, base_ref_name=b_ref)
            return False

    # Only return True if all pending tasks cannot be unblocked (all their bases are failed or missing)
    return True
