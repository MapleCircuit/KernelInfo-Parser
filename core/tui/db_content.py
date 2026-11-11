"""Read-mostly inspection of the configured database for the TUI's DB Content Viewer.

`open_db_session()` connects to the database `cfg` names (through a temporary SSH forward when the
tunnel is enabled) without changing the process's active configuration. `collect_snapshot()` gathers,
through `BaseDBEngine` spec reads only, what the viewer's tabs show: version parse states, repositories
and their refs, mailing-list / CVE data presence, and table sizes. `clean_versions()` removes the rows
of interrupted version runs (`core.ingest_state.clean_interrupted_version()`).
"""

from __future__ import annotations

import copy
import re
import time
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Any

from core.ingest_state import INGEST_DONE, INGEST_PARSING
from db_engine.spec import ge, gt

# Version parse states (`VersionRow.state`)
STATE_DONE = "done"
STATE_INTERRUPTED = "interrupted"
STATE_IN_PROGRESS = "in_progress"
STATE_LEGACY = "legacy"
STATE_NEW = "new"
PARTIAL_STATES = (STATE_INTERRUPTED, STATE_IN_PROGRESS, STATE_LEGACY)
STATE_LABELS = {
    STATE_DONE: "parsed",
    STATE_INTERRUPTED: "partial (interrupted)",
    STATE_IN_PROGRESS: "partial (in progress)",
    STATE_LEGACY: "partial? (legacy)",
    STATE_NEW: "not parsed",
}

# m_git_ref.ref_type
REF_TYPE_LABELS = {1: "tag", 2: "branch", 3: "commit"}

# A PARSING state row whose worker heartbeated this recently belongs to a live run (core/CLAUDE.md §6)
HEARTBEAT_WINDOW_S = 180

ML_TABLES = ("m_commit_email", "m_commit_link")
CVE_TABLES = ("m_cve_source", "m_cve", "m_cve_commit", "m_cve_file", "m_cve_commit_hash")

_TRUTHY = (True, "true", "True", 1, "1", "yes", "on")


@dataclass
class VersionRow:
    """One registered ref (`m_git_ref`) or orphan state row and the parse state of its commit."""

    network_id: int
    repo: str
    ref_name: str
    ref_type: str
    commit_id: int
    state: str
    updated_at: int = 0
    worker_id: str = ""

    @property
    def cleanable(self) -> bool:
        """True for a stale interrupted run whose rows `clean_versions()` may remove."""
        return self.state == STATE_INTERRUPTED


@dataclass
class RepoRow:
    """One repository (`m_repository`) with ref counts and parse-state totals."""

    repo_id: int
    network: str
    name: str
    git_url: str
    default_branch: str
    tags: int = 0
    branches: int = 0
    commits: int = 0
    parsed: int = 0
    partial: int = 0
    branch_names: list[str] = field(default_factory=list)


@dataclass
class FeedTable:
    """Presence and estimated size of one table backing an external data feed."""

    name: str
    exists: bool
    has_rows: bool
    rows: int = 0
    data_bytes: int = 0


@dataclass
class TableRow:
    """Catalog statistics of one table (`BaseDBEngine.table_stats()`; InnoDB row counts are estimates)."""

    name: str
    rows: int
    data_bytes: int
    index_bytes: int
    in_schema: bool

    @property
    def total_bytes(self) -> int:
        return self.data_bytes + self.index_bytes


@dataclass
class DBSnapshot:
    """Everything the DB Content Viewer shows, read in one session."""

    target: str
    versions: list[VersionRow] = field(default_factory=list)
    repos: list[RepoRow] = field(default_factory=list)
    mailing_list: list[FeedTable] = field(default_factory=list)
    cve: list[FeedTable] = field(default_factory=list)
    cve_sources: list[dict[str, Any]] = field(default_factory=list)
    tables: list[TableRow] = field(default_factory=list)
    missing_tables: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    loaded_at: float = 0.0

    def state_counts(self) -> dict[str, int]:
        """Number of version rows per state."""
        counts = dict.fromkeys(STATE_LABELS, 0)
        for v in self.versions:
            counts[v.state] = counts.get(v.state, 0) + 1
        return counts

    def cleanable_versions(self) -> list[VersionRow]:
        """Interrupted version runs, one per (network, commit)."""
        seen: set[tuple[int, int]] = set()
        out: list[VersionRow] = []
        for v in self.versions:
            key = (v.network_id, v.commit_id)
            if v.cleanable and key not in seen:
                seen.add(key)
                out.append(v)
        return out

    def totals(self) -> tuple[int, int, int]:
        """(rows, data_bytes, index_bytes) summed over every table."""
        return (
            sum(t.rows for t in self.tables),
            sum(t.data_bytes for t in self.tables),
            sum(t.index_bytes for t in self.tables),
        )


# --- Formatting ---------------------------------------------------------------------------------


def format_bytes(n: int | float) -> str:
    """Human-readable byte size (`1.5 GiB`)."""
    value = float(n or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def format_count(n: int) -> str:
    """Thousands-separated integer."""
    return f"{int(n or 0):,}"


def version_sort_key(name: str) -> tuple:
    """Natural sort key: `v3.2` < `v3.10`, release candidates after their base digits."""
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in re.split(r"(\d+)", name) if part)


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return "" if value is None else str(value)


# --- Session ------------------------------------------------------------------------------------


@contextmanager
def open_db_session(cfg: dict[str, Any]) -> Iterator[Any]:
    """Yield an open `BaseDBEngine` for the database `cfg["database"]` names.

    An enabled SSH tunnel is reused when this process already runs one, otherwise a temporary forward
    is opened for the session (`core.tui.testing.open_test_tunnel`). The connection settings are
    exported only for the session (`core.config.scoped_environ`), so the active configuration and the
    environment the pipeline later reads stay unchanged.
    """
    from core.config import scoped_environ
    from core.tui.testing import open_test_tunnel
    from db_engine import get_db_engine

    view_cfg = copy.deepcopy(cfg if isinstance(cfg, dict) else {})
    db_cfg = view_cfg.setdefault("database", {})
    if not isinstance(db_cfg, dict):
        db_cfg = view_cfg["database"] = {}
    ssh_cfg = db_cfg.get("ssh_tunnel") if isinstance(db_cfg.get("ssh_tunnel"), dict) else {}
    engine_cls = get_db_engine(str(db_cfg.get("engine") or "mariadb"))

    with ExitStack() as stack:
        if ssh_cfg.get("enabled") in _TRUTHY:
            local_port = stack.enter_context(open_test_tunnel(ssh_cfg, "database"))
            db_cfg["host"] = "127.0.0.1"
            db_cfg["port"] = local_port
        stack.enter_context(scoped_environ(view_cfg))
        db = engine_cls()
        stack.callback(_close_quietly, db)
        with db:
            yield db


def _close_quietly(db: Any) -> None:
    try:
        db.close()
    except Exception:
        pass


def describe_target(cfg: dict[str, Any]) -> str:
    """One-line description of the database `cfg` connects to."""
    from core.tui.screens.table_reset import describe_reset_target

    return describe_reset_target(cfg if isinstance(cfg, dict) else {})


# --- Collection ---------------------------------------------------------------------------------


def collect_snapshot(db: Any, target: str = "", now: float | None = None) -> DBSnapshot:
    """Read every viewer section from `db`; a failing section is reported in `errors`, not raised."""
    snap = DBSnapshot(target=target, loaded_at=time.time() if now is None else now)
    now_s = int(snap.loaded_at)

    try:
        snap.tables, snap.missing_tables = collect_tables(db)
    except Exception as exc:
        snap.errors.append(f"Table statistics: {exc}")
    existing = {t.name for t in snap.tables}
    stats = {t.name: t for t in snap.tables}

    try:
        snap.versions, snap.repos = collect_versions(db, now_s)
    except Exception as exc:
        snap.errors.append(f"Versions & repositories: {exc}")

    try:
        snap.mailing_list = collect_feed(db, ML_TABLES, existing, stats)
    except Exception as exc:
        snap.errors.append(f"Mailing list: {exc}")
    try:
        snap.cve = collect_feed(db, CVE_TABLES, existing, stats)
        if "m_cve_source" in existing or not existing:
            snap.cve_sources = collect_cve_sources(db)
    except Exception as exc:
        snap.errors.append(f"CVE: {exc}")
    return snap


def collect_tables(db: Any) -> tuple[list[TableRow], list[str]]:
    """Per-table catalog statistics (largest first) and the schema tables missing from the database."""
    from core.DBLayout import TABLES

    schema = [t.table_name for t in TABLES]
    schema_set = set(schema)
    rows = [
        TableRow(
            name=_text(s.name),
            rows=int(s.rows or 0),
            data_bytes=int(s.data_bytes or 0),
            index_bytes=int(s.index_bytes or 0),
            in_schema=_text(s.name) in schema_set,
        )
        for s in db.table_stats()
    ]
    rows.sort(key=lambda t: (-t.total_bytes, t.name))
    present = {t.name for t in rows}
    if not present:
        present = set(db.existing_tables(schema))
    missing = [name for name in schema if name not in present]
    return rows, missing


def collect_versions(db: Any, now_s: int) -> tuple[list[VersionRow], list[RepoRow]]:
    """Parse state of every registered ref plus orphan `m_ingest_state` rows, and per-repository totals."""
    networks = {
        int(r[0]): _text(r[1]) for r in db.select_rows("m_git_network", ["network_id", "name"], as_dict=False)
    }
    repo_rows = db.select_rows(
        "m_repository", ["repo_id", "network_id", "name", "git_url", "default_branch"], as_dict=False
    )
    repo_net: dict[int, int] = {}
    repos: dict[int, RepoRow] = {}
    for repo_id, network_id, name, git_url, default_branch in repo_rows:
        repo_id, network_id = int(repo_id), int(network_id)
        repo_net[repo_id] = network_id
        repos[repo_id] = RepoRow(
            repo_id=repo_id,
            network=networks.get(network_id, str(network_id)),
            name=_text(name),
            git_url=_text(git_url),
            default_branch=_text(default_branch),
        )

    states: dict[tuple[int, int], tuple[int, int, str]] = {}
    for network_id, commit_id, status, updated_at, worker_id in db.select_rows(
        "m_ingest_state", ["network_id", "commit_id", "status", "updated_at", "worker_id"], as_dict=False
    ):
        states[(int(network_id), int(commit_id))] = (int(status), int(updated_at or 0), _text(worker_id))

    live_workers: set[str] = set()
    if any(s[0] == INGEST_PARSING and s[2] for s in states.values()):
        live_workers = {
            _text(n)
            for n in db.select_values("m_parse_node", "node_id", {"heartbeat_at": ge(now_s - HEARTBEAT_WINDOW_S)})
        }

    refs = db.select_rows(
        "m_git_ref", ["repo_id", "ref_type", "ref_name", "commit_id"], {"commit_id": gt(0)}, as_dict=False
    )
    unstated = sorted(
        {int(cid) for repo_id, _t, _n, cid in refs if (repo_net.get(int(repo_id), 0), int(cid)) not in states}
    )
    checkpointed: set[int] = set()
    if unstated:
        checkpointed = {
            int(c)
            for c in db.select_values("m_bridge_checkpoint", "commit_id", {"commit_id": unstated}, distinct=True)
        }

    versions: list[VersionRow] = []
    referenced: set[tuple[int, int]] = set()
    for repo_id, ref_type, ref_name, commit_id in refs:
        repo_id, commit_id = int(repo_id), int(commit_id)
        network_id = repo_net.get(repo_id, 0)
        referenced.add((network_id, commit_id))
        status, updated_at, worker = states.get((network_id, commit_id), (0, 0, ""))
        state = _classify(status, worker, live_workers, commit_id in checkpointed)
        repo = repos.get(repo_id)
        rtype = REF_TYPE_LABELS.get(int(ref_type or 0), f"type {ref_type}")
        versions.append(
            VersionRow(
                network_id=network_id,
                repo=repo.name if repo else str(repo_id),
                ref_name=_text(ref_name),
                ref_type=rtype,
                commit_id=commit_id,
                state=state,
                updated_at=updated_at,
                worker_id=worker,
            )
        )
        if repo is not None:
            if rtype == "tag":
                repo.tags += 1
            elif rtype == "branch":
                repo.branches += 1
                repo.branch_names.append(_text(ref_name))
            else:
                repo.commits += 1
            if state == STATE_DONE:
                repo.parsed += 1
            elif state in PARTIAL_STATES:
                repo.partial += 1

    # State rows no ref points at (the ref was renamed or removed after the run started)
    for (network_id, commit_id), (status, updated_at, worker) in states.items():
        if (network_id, commit_id) in referenced:
            continue
        versions.append(
            VersionRow(
                network_id=network_id,
                repo=networks.get(network_id, str(network_id)),
                ref_name=f"commit_id {commit_id}",
                ref_type="no ref",
                commit_id=commit_id,
                state=_classify(status, worker, live_workers, False),
                updated_at=updated_at,
                worker_id=worker,
            )
        )

    versions.sort(key=lambda v: (v.repo, v.ref_type, version_sort_key(v.ref_name)))
    for repo in repos.values():
        repo.branch_names.sort()
    repo_list = sorted((r for r in repos.values() if r.repo_id or r.tags or r.branches), key=lambda r: r.name)
    return versions, repo_list


def _classify(status: int, worker: str, live_workers: set[str], checkpointed: bool) -> str:
    if status == INGEST_DONE:
        return STATE_DONE
    if status == INGEST_PARSING:
        return STATE_IN_PROGRESS if worker and worker in live_workers else STATE_INTERRUPTED
    return STATE_LEGACY if checkpointed else STATE_NEW


def collect_feed(db: Any, names: Sequence[str], existing: set[str], stats: dict[str, TableRow]) -> list[FeedTable]:
    """Presence (exact, `row_exists`) and estimated size of each table in `names`."""
    if not existing:
        existing = set(db.existing_tables(list(names)))
    out: list[FeedTable] = []
    for name in names:
        exists = name in existing
        has_rows = bool(exists and db.row_exists(name))
        st = stats.get(name)
        out.append(
            FeedTable(
                name=name,
                exists=exists,
                has_rows=has_rows,
                rows=st.rows if st else 0,
                data_bytes=st.total_bytes if st else 0,
            )
        )
    return out


def collect_cve_sources(db: Any) -> list[dict[str, Any]]:
    """Rows of `m_cve_source` (the CVE list snapshot ingested)."""
    out: list[dict[str, Any]] = []
    for name, layout, head, fmt, records, built_at in db.select_rows(
        "m_cve_source", ["name", "layout", "head", "format", "records", "built_at"], as_dict=False
    ):
        out.append(
            {
                "name": _text(name),
                "layout": _text(layout),
                "head": head.hex() if isinstance(head, (bytes, bytearray)) else _text(head),
                "format": int(fmt or 0),
                "records": int(records or 0),
                "built_at": int(built_at or 0),
            }
        )
    return out


# --- Cleaning -----------------------------------------------------------------------------------


@dataclass
class CleanResult:
    """Outcome of cleaning one interrupted version run."""

    version: VersionRow
    ok: bool
    method: str = ""
    error: str = ""


def clean_versions(db: Any, versions: Sequence[VersionRow]) -> list[CleanResult]:
    """Remove the rows of each interrupted run in `versions` (other states are skipped as not cleanable)."""
    from core.DBLayout import TABLES
    from core.ingest_state import clean_interrupted_version

    results: list[CleanResult] = []
    seen: set[tuple[int, int]] = set()
    for v in versions:
        key = (v.network_id, v.commit_id)
        if key in seen:
            continue
        seen.add(key)
        if not v.cleanable:
            results.append(CleanResult(v, False, error=f"state is {STATE_LABELS.get(v.state, v.state)}"))
            continue
        stale = _current_state_problem(db, v)
        if stale:
            results.append(CleanResult(v, False, error=stale))
            continue
        try:
            method = clean_interrupted_version(db, v.network_id, v.commit_id, TABLES)
            results.append(CleanResult(v, True, method=method))
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                pass
            results.append(CleanResult(v, False, error=str(exc)))
    return results


def _current_state_problem(db: Any, v: VersionRow) -> str:
    """Re-read the state of `v` right before cleaning; a non-empty reason means it is no longer interrupted."""
    row = db.select_one(
        "m_ingest_state",
        ["status", "worker_id"],
        {"network_id": v.network_id, "commit_id": v.commit_id},
        as_dict=False,
    )
    if row is None or int(row[0]) != INGEST_PARSING:
        return "state changed since the snapshot (no longer interrupted); reload and retry"
    worker = _text(row[1])
    if worker and db.row_exists(
        "m_parse_node", {"node_id": worker, "heartbeat_at": ge(int(time.time()) - HEARTBEAT_WINDOW_S)}
    ):
        return f"worker '{worker}' is parsing it now"
    return ""
