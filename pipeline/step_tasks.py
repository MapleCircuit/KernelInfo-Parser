"""pipeline/step_tasks.py - Independent steps as distributed auxiliary tasks (core/API.md section 11.4).

A version ingest (`update()`) runs a few steps whose data is independent of the version's own rows: mailing list
resolution (Step 6.5), KAPI catalogs, and - in pre-parse tasks - the release insights. Repository-level steps (the
CVE list and release statistics) run once per repository. `STEPS` registers each one with the repository feature(s)
enabling it, the worker capability it needs, its priority, and how a node probes and runs it.

- A step whose feature is disabled (`disabled`) or which has nothing to do (`skipped`) produces nothing.
- A step that is enabled but cannot run on this node (`incapable`) or fails (`failed`) is recorded in
  `gp.step_outcomes`; in distributed mode the version task still completes and `enqueue_step_followups()` publishes
  one pending `step_*` task per such step, which only workers advertising that capability claim.
- Repository-level steps are published as `repo_*` tasks (`repo_task_rows()` / `enqueue_repo_tasks()`) by the
  daemon and the master, and claimed by capable workers before stable version tasks (`PRIORITY_REPO`).
- `local_step_capabilities()` lists the capabilities a node advertises (`distributed_worker_step_caps`); missing a
  capability never keeps a node from claiming version tasks.
"""
from __future__ import annotations

import hashlib
import subprocess as sp
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core import distributed
from core.config import (
    get_config,
    get_mailing_list_config,
    get_ssh_tunnel_config,
    is_kernel_project,
    repository_project,
    resolve_repo_features,
)
from core.globalstuff import COLOR, G
from pipeline.git_network import feature_enabled, kernel_steps_apply
from pipeline.state import gp, logger

STATUS_OK = "ok"
STATUS_DISABLED = "disabled"
STATUS_SKIPPED = "skipped"
STATUS_INCAPABLE = "incapable"
STATUS_FAILED = "failed"
STATUS_DEFERRED = "deferred"
FOLLOWUP_STATUSES = (STATUS_INCAPABLE, STATUS_FAILED)

SCOPE_VERSION = "version"
SCOPE_REPO = "repo"

# Seconds before a failed (or deferred) auxiliary task may be claimed again: base * 2**retry_count, capped
RETRY_BASE_DELAY = 60
RETRY_MAX_DELAY = 3600
DEFAULT_CVE_TASK_INTERVAL = 86400


@dataclass(frozen=True)
class StepResult:
    """Outcome of one step run: a `STATUS_*` value and a human-readable detail (error, reason)."""

    status: str
    detail: str = ""


@dataclass(frozen=True)
class StepContext:
    """What a step task runs against (taken from its `distributed_tasks` row)."""

    repo_path: str
    repo_id: int | None
    network_id: int
    repo_name: str
    ref_name: str = ""
    base_ref_name: str = ""
    commit_id: int = 0


@dataclass(frozen=True)
class StepDef:
    """One independent step. `capability` is the name a worker advertises and a task requires."""

    name: str
    task_type: str
    scope: str
    features: tuple[str, ...]
    priority: int
    probe: Callable[[str], tuple[bool, str]]
    run: Callable[[StepContext], StepResult]
    kernel_only: bool = False
    max_attempts: int = 5
    extra_enabled: Callable[[], bool] | None = None

    @property
    def capability(self) -> str:
        return self.name

    def enabled(self, feature_lookup: Callable[[str], bool], *, is_kernel: bool = True) -> bool:
        """True when the step is enabled: a kernel tree when `kernel_only`, any of `features`, and `extra_enabled`."""
        if self.kernel_only and not is_kernel:
            return False
        if not any(feature_lookup(f) for f in self.features):
            return False
        return self.extra_enabled() if self.extra_enabled is not None else True


# ---------------------------------------------------------------------------
# Probes: can this node run the step for the repository at `repo_path`?
# ---------------------------------------------------------------------------

def _is_git_dir(path: str | Path) -> bool:
    p = Path(path)
    return (p / ".git").exists() or (p / "HEAD").is_file()


def _probe_git_repo(repo_path: str) -> tuple[bool, str]:
    if repo_path and _is_git_dir(repo_path):
        return True, ""
    return False, f"no git repository at '{repo_path}'"


def _mailing_list_enabled() -> bool:
    return bool(get_mailing_list_config().get("enabled", True))


def _probe_mailing_list(_repo_path: str) -> tuple[bool, str]:
    """The archive is local (`mailing_list.storage_dir`) or the tunnelled daemon answers."""
    cfg = get_mailing_list_config()
    if not get_ssh_tunnel_config("mailing_list").get("enabled", False):
        storage = Path(str(cfg.get("storage_dir", "/srv/lore") or "/srv/lore"))
        if storage.exists():
            return True, f"local archive {storage}"
        return False, f"no local mailing list archive at '{storage}' and the SSH tunnel is disabled"
    try:
        from parser.mailing_list.ml_client import MailingListClient
        client = MailingListClient()
        client.close()
    except Exception as exc:  # noqa: BLE001 - any failure means this node cannot resolve emails
        return False, str(exc)[:255]
    return True, "tunnelled daemon"


def _probe_cve(_repo_path: str) -> tuple[bool, str]:
    from parser.cve import cve_ingest
    repo = cve_ingest.repo_dir()
    if repo is None:
        return False, "no CVE list clone (cve.repo_dir)"
    return True, str(repo)


# ---------------------------------------------------------------------------
# Runners: execute a step task
# ---------------------------------------------------------------------------

def _result_from_summary(summary: Mapping[str, Any] | None, *, none_status: str = STATUS_DISABLED) -> StepResult:
    """Map an `external_steps` summary (`None`: disabled; `state`: error / skipped / deferred / ...) to a result."""
    if summary is None:
        return StepResult(none_status)
    state = str(summary.get("state") or "")
    if state == "error":
        return StepResult(STATUS_FAILED, str(summary.get("error") or "error"))
    if state == "deferred":
        return StepResult(STATUS_DEFERRED, "deferred")
    if state == "skipped":
        return StepResult(STATUS_SKIPPED, str(summary.get("reason") or ""))
    return StepResult(STATUS_OK, state)


def _run_mailing_list(ctx: StepContext) -> StepResult:
    from pipeline.mailing_list_steps import resolve_mailing_list_range
    commit_range = f"{ctx.base_ref_name}..{ctx.ref_name}" if ctx.base_ref_name else ctx.ref_name
    try:
        staged = resolve_mailing_list_range(ctx.repo_path, commit_range, network_id=ctx.network_id)
    except Exception as exc:  # noqa: BLE001 - reported as a step failure (retried with backoff)
        return StepResult(STATUS_FAILED, str(exc))
    if staged is None:
        return StepResult(STATUS_SKIPPED, "no ingested commit in range")
    return StepResult(STATUS_OK, f"{staged} row(s)")


def _run_kapi(ctx: StepContext) -> StepResult:
    from pipeline.external_steps import processing_kapi
    from pipeline.git_sync import git_repo_lock
    with git_repo_lock(ctx.repo_path, exclusive=False):
        summary = processing_kapi(ctx.repo_path, ctx.repo_id, ctx.ref_name, commit_id=ctx.commit_id or None)
    return _result_from_summary(summary)


def _run_insights(ctx: StepContext) -> StepResult:
    from pipeline.external_steps import processing_unified_preparse
    from pipeline.git_sync import git_repo_lock
    with git_repo_lock(ctx.repo_path, exclusive=False):
        summary = processing_unified_preparse(ctx.repo_path, ctx.repo_id, tags=[ctx.ref_name], include_api=False)
    if str(summary.get("state") or "") == "skipped":
        return StepResult(STATUS_DISABLED, str(summary.get("reason") or ""))
    errors = [str(e.get("error") or "") for e in summary.get("errors") or ()]
    if errors:
        return StepResult(STATUS_FAILED, "; ".join(errors))
    return StepResult(STATUS_OK, str(summary.get("state") or ""))


def _run_release_stats(ctx: StepContext) -> StepResult:
    from pipeline.external_steps import processing_release_stats
    return _result_from_summary(processing_release_stats(ctx.repo_path, int(ctx.repo_id or 0)))


def _run_cve(ctx: StepContext) -> StepResult:
    from pipeline.external_steps import processing_cve_list
    cve_cfg = get_config().get("cve") or {}
    summary = processing_cve_list(
        force=False,
        kernel_repo=ctx.repo_path,
        repo_id=ctx.repo_id,
        pull=bool(cve_cfg.get("pull", False)),
    )
    return _result_from_summary(summary, none_status=STATUS_INCAPABLE)


STEPS: dict[str, StepDef] = {
    step.name: step
    for step in (
        StepDef(
            name="mailing_list",
            task_type=distributed.TASK_TYPE_STEP_MAILING_LIST,
            scope=SCOPE_VERSION,
            features=("mail",),
            priority=distributed.PRIORITY_FOLLOWUP,
            probe=_probe_mailing_list,
            run=_run_mailing_list,
            extra_enabled=_mailing_list_enabled,
        ),
        StepDef(
            name="kapi",
            task_type=distributed.TASK_TYPE_STEP_KAPI,
            scope=SCOPE_VERSION,
            features=("api",),
            priority=distributed.PRIORITY_FOLLOWUP,
            probe=_probe_git_repo,
            run=_run_kapi,
        ),
        StepDef(
            name="insights",
            task_type=distributed.TASK_TYPE_STEP_INSIGHTS,
            scope=SCOPE_VERSION,
            features=("insights",),
            priority=distributed.PRIORITY_FOLLOWUP,
            probe=_probe_git_repo,
            run=_run_insights,
            kernel_only=True,
        ),
        StepDef(
            name="release_stats",
            task_type=distributed.TASK_TYPE_REPO_RELEASE_STATS,
            scope=SCOPE_REPO,
            features=("commits", "insights"),
            priority=distributed.PRIORITY_REPO,
            probe=_probe_git_repo,
            run=_run_release_stats,
        ),
        StepDef(
            name="cve",
            task_type=distributed.TASK_TYPE_REPO_CVE,
            scope=SCOPE_REPO,
            features=("cve",),
            priority=distributed.PRIORITY_REPO,
            probe=_probe_cve,
            run=_run_cve,
            kernel_only=True,
        ),
    )
}
STEPS_BY_TASK_TYPE: dict[str, StepDef] = {s.task_type: s for s in STEPS.values()}


def step_for_task_type(task_type: str) -> StepDef | None:
    """The step a `distributed_tasks.task_type` runs, or None for version / pre-parse tasks."""
    return STEPS_BY_TASK_TYPE.get(str(task_type or ""))


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

_PROBE_CACHE: dict[tuple[str, str], tuple[bool, str]] = {}


def probe_step(step: StepDef, repo_path: str, *, refresh: bool = False) -> tuple[bool, str]:
    """Cached `step.probe(repo_path)`; `refresh` probes again (capability re-registration)."""
    key = (step.name, str(repo_path))
    if refresh or key not in _PROBE_CACHE:
        try:
            _PROBE_CACHE[key] = step.probe(str(repo_path))
        except Exception as exc:  # noqa: BLE001 - a crashing probe means the node cannot run the step
            _PROBE_CACHE[key] = (False, str(exc)[:255])
    return _PROBE_CACHE[key]


def local_step_capabilities(
    repo_path: str,
    repo_entry: Mapping[str, Any] | None,
    *,
    is_kernel: bool | None = None,
    refresh: bool = True,
) -> list[dict[str, str]]:
    """Step capabilities this node advertises for the repository: the step is enabled in this node's
    configuration for it and its probe passes. Logs the enabled steps the node cannot run."""
    features = resolve_repo_features(dict(repo_entry) if repo_entry else None)
    kernel = is_kernel_project(repository_project(dict(repo_entry) if repo_entry else None)) if is_kernel is None else is_kernel
    caps: list[dict[str, str]] = []
    for step in STEPS.values():
        if not step.enabled(lambda f: bool(features.get(f, False)), is_kernel=kernel):
            continue
        ok, detail = probe_step(step, repo_path, refresh=refresh)
        if ok:
            caps.append({"capability": step.capability, "detail": detail})
        else:
            logger.warning(COLOR.yellow(
                f"[Distributed] Step '{step.name}' unavailable on this node for '{repo_path}' ({detail}); "
                "its tasks are left to capable nodes."
            ))
    return caps


# ---------------------------------------------------------------------------
# Inline outcomes of a version ingest
# ---------------------------------------------------------------------------

def reset_step_outcomes() -> None:
    """Forget the step outcomes of the previous version (called at the start of `update()`)."""
    gp.step_outcomes = {}


def step_outcomes() -> dict[str, StepResult]:
    out = getattr(gp, "step_outcomes", None)
    if not isinstance(out, dict):
        out = {}
        gp.step_outcomes = out
    return out


def record_step_outcome(name: str, result: StepResult) -> StepResult:
    """Record the outcome of step `name` for the version being ingested."""
    step_outcomes()[name] = result
    if result.status in FOLLOWUP_STATUSES:
        logger.warning(COLOR.yellow(
            f"[Steps] '{name}' {result.status} for '{getattr(gp, 'Version_Name', '')}': {result.detail}"
        ))
    return result


def inline_step_gate(name: str, repo_path: str | None = None) -> StepResult | None:
    """Decide whether the inline step `name` runs for the active repository: a `disabled` result (recorded)
    when its feature is off, an `incapable` result (recorded) when this distributed node lacks its capability,
    else None (run it, then record the outcome)."""
    step = STEPS[name]
    if not step.enabled(feature_enabled, is_kernel=kernel_steps_apply()):
        return record_step_outcome(name, StepResult(STATUS_DISABLED))
    if getattr(G, "distributed", False):
        ok, detail = probe_step(step, str(repo_path or ""))
        if not ok:
            return record_step_outcome(name, StepResult(STATUS_INCAPABLE, detail))
    return None


def kapi_outcome(summary: Mapping[str, Any] | None) -> StepResult:
    """Outcome of the inline KAPI step from its `processing_kapi` summary."""
    return _result_from_summary(summary)


def preparse_outcomes(summary: Mapping[str, Any] | None) -> dict[str, StepResult]:
    """Per-step outcomes of a pre-parse task from its per-tag errors: `kapi:` errors are the KAPI step's,
    every other error (`deltas:`, `segment:`, persisting) the insights step's."""
    errs: dict[str, list[str]] = {"insights": [], "kapi": []}
    for err in (summary or {}).get("errors") or ():
        text = str(err.get("error") if isinstance(err, Mapping) else err)
        errs["kapi" if text.startswith("kapi:") else "insights"].append(text)
    return {name: StepResult(STATUS_FAILED, "; ".join(msgs)) for name, msgs in errs.items() if msgs}


# ---------------------------------------------------------------------------
# Queue rows
# ---------------------------------------------------------------------------

def followup_rows(task: Mapping[str, Any], outcomes: Mapping[str, StepResult], *, now: int | None = None) -> list[tuple]:
    """`enqueue_distributed_tasks` rows (17 columns) of the follow-up tasks for the `incapable` / `failed`
    version steps of `task` (a claimed `distributed_tasks` row): same repository, ref, track, base and commit,
    the step's task type, priority and required capability."""
    ts = int(time.time()) if now is None else int(now)
    rows: list[tuple] = []
    for name, result in outcomes.items():
        step = STEPS.get(name)
        if step is None or step.scope != SCOPE_VERSION or result.status not in FOLLOWUP_STATUSES:
            continue
        rows.append((
            int(task.get("network_id") or 1),
            int(task.get("repo_id") or 0),
            str(task.get("repo_name") or ""),
            step.task_type,
            str(task.get("track_name") or ""),
            str(task.get("ref_name") or ""),
            str(task.get("target_sha") or ""),
            int(task.get("commit_id") or 0),
            str(task.get("base_ref_name") or ""),
            int(task.get("base_commit_id") or 0),
            step.priority,
            int(task.get("seq_order") or 0),
            distributed.DIST_STATUS_PENDING,
            ts,
            ts,
            step.capability,
            0,
        ))
    return rows


def enqueue_step_followups(db: Any, task: Mapping[str, Any], outcomes: Mapping[str, StepResult] | None = None) -> int:
    """Publish the follow-up tasks of `task`'s failed / incapable steps (default: `gp.step_outcomes`)."""
    rows = followup_rows(task, step_outcomes() if outcomes is None else outcomes)
    if not rows or db is None:
        return 0
    written = int(db.enqueue_distributed_tasks(rows) or 0)
    logger.info(COLOR.cyan(
        f"[Steps] Queued {len(rows)} follow-up task(s) for '{task.get('ref_name')}': "
        f"{', '.join(r[3] for r in rows)}."
    ))
    return written


def _git_lines(repo: str | Path, *args: str) -> list[str]:
    try:
        res = sp.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False, timeout=60)
    except (OSError, sp.SubprocessError):
        return []
    return res.stdout.splitlines() if res.returncode == 0 else []


def repo_fingerprint(step: StepDef, repo_path: str, *, now: int | None = None) -> str:
    """`target_sha` of a repository-level task: a digest of the repository's tags, and for the CVE step the
    local CVE list HEAD (when this node has the clone) and the `cve.task_interval` period, so a task reopens
    when its input moved (or, for the CVE list, once per period)."""
    h = hashlib.sha256()
    h.update("\n".join(sorted(_git_lines(repo_path, "for-each-ref", "--format=%(refname) %(objectname)", "refs/tags"))).encode())
    if step.name == "cve":
        from parser.cve import cve_ingest
        cve_repo = cve_ingest.repo_dir()
        if cve_repo is not None:
            h.update(("\0" + "".join(_git_lines(cve_repo, "rev-parse", "HEAD"))).encode())
        interval = int((get_config().get("cve") or {}).get("task_interval", DEFAULT_CVE_TASK_INTERVAL) or 0)
        if interval > 0:
            ts = int(time.time()) if now is None else int(now)
            h.update(f"\0{ts // interval}".encode())
    return h.hexdigest()[:64]


def repo_task_rows(
    network_id: int,
    repo_id: int,
    repo_name: str,
    repo_path: str,
    *,
    feature_lookup: Callable[[str], bool],
    is_kernel: bool,
    skip_steps: Collection[str] = (),
    now: int | None = None,
) -> list[tuple]:
    """`enqueue_distributed_tasks` rows of the enabled repository-level steps not in `skip_steps` (`ref_name`
    `REPO_TASK_REF`, no track, `target_sha` = `repo_fingerprint()`)."""
    ts = int(time.time()) if now is None else int(now)
    rows: list[tuple] = []
    for step in STEPS.values():
        if step.scope != SCOPE_REPO or step.name in skip_steps or not step.enabled(feature_lookup, is_kernel=is_kernel):
            continue
        rows.append((
            int(network_id), int(repo_id), str(repo_name), step.task_type, "", distributed.REPO_TASK_REF,
            repo_fingerprint(step, repo_path, now=ts), 0, "", 0, step.priority, 0,
            distributed.DIST_STATUS_PENDING, ts, ts, step.capability, 0,
        ))
    return rows


def enqueue_repo_tasks(
    db: Any,
    network_id: int,
    repo_id: int,
    repo_name: str,
    repo_path: str,
    *,
    feature_lookup: Callable[[str], bool],
    is_kernel: bool,
    skip_steps: Collection[str] = (),
    now: int | None = None,
) -> int:
    """Publish the repository-level tasks whose row is missing or whose fingerprint moved (a claimed task is
    left to its owner); returns the number of rows written."""
    rows = repo_task_rows(
        network_id, repo_id, repo_name, repo_path, feature_lookup=feature_lookup, is_kernel=is_kernel,
        skip_steps=skip_steps, now=now,
    )
    if not rows or db is None:
        return 0
    existing = {
        str(r["task_type"]): r
        for r in db.select_rows(
            "distributed_tasks",
            ["task_type", "target_sha", "status"],
            {
                "network_id": int(network_id),
                "repo_name": str(repo_name),
                "task_type": [r[3] for r in rows],
                "ref_name": distributed.REPO_TASK_REF,
            },
            as_dict=True,
        )
    }
    delta = []
    for row in rows:
        ex = existing.get(row[3])
        if ex is None or (str(ex.get("status")) != distributed.DIST_STATUS_CLAIMED and str(ex.get("target_sha") or "") != row[6]):
            delta.append(row)
    if not delta:
        return 0
    written = int(db.enqueue_distributed_tasks(delta) or 0)
    logger.info(COLOR.cyan(f"[Steps] Queued repository task(s) for '{repo_name}': {', '.join(r[3] for r in delta)}."))
    return written


# ---------------------------------------------------------------------------
# Executing a claimed auxiliary task
# ---------------------------------------------------------------------------

def retry_delay(retry_count: int) -> int:
    """Backoff before a failed or deferred auxiliary task is claimable again."""
    return int(min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * (2 ** max(0, int(retry_count or 0)))))


def run_step_task(task: Mapping[str, Any], repo_path: str, repo_id: int | None) -> StepResult:
    """Run the step of a claimed auxiliary task. The step is re-checked against this node's configuration
    (`disabled`) and capability (`incapable`) first."""
    step = step_for_task_type(str(task.get("task_type") or ""))
    if step is None:
        return StepResult(STATUS_FAILED, f"unknown step task type '{task.get('task_type')}'")
    if not step.enabled(feature_enabled, is_kernel=kernel_steps_apply()):
        return StepResult(STATUS_DISABLED, "feature disabled on this node")
    ok, detail = probe_step(step, repo_path, refresh=True)
    if not ok:
        return StepResult(STATUS_INCAPABLE, detail)
    ctx = StepContext(
        repo_path=str(repo_path),
        repo_id=repo_id,
        network_id=int(task.get("network_id") or 1),
        repo_name=str(task.get("repo_name") or ""),
        ref_name=str(task.get("ref_name") or ""),
        base_ref_name=str(task.get("base_ref_name") or ""),
        commit_id=int(task.get("commit_id") or 0),
    )
    try:
        return step.run(ctx)
    except Exception as exc:  # noqa: BLE001 - a step failure is retried with backoff, never fatal
        logger.error(f"[Steps] '{step.name}' task {task.get('task_id')} raised: {exc}", exc_info=True)
        return StepResult(STATUS_FAILED, str(exc))


def finish_step_task(scheduler: Any, task: Mapping[str, Any], result: StepResult, *, db: Any = None) -> str:
    """Settle a claimed auxiliary task from its result: `ok` / `skipped` complete it (done, or
    nothing to do), `failed` records a retry with backoff (terminal after the step's `max_attempts`, never
    cascading), `deferred` / `incapable` / `disabled` (this node's capability or configuration changed since it
    registered) release it with backoff for another node. Returns the action."""
    t_id = int(task["task_id"])
    step = step_for_task_type(str(task.get("task_type") or ""))
    delay = retry_delay(int(task.get("retry_count") or 0))
    if result.status in (STATUS_OK, STATUS_SKIPPED):
        scheduler.complete(t_id, int(task.get("commit_id") or 0), ref_name=str(task.get("ref_name") or ""), db=db)
        return "completed"
    if result.status == STATUS_FAILED:
        scheduler.fail(
            t_id, result.detail or "step failed", max_attempts=step.max_attempts if step else 3, retry_delay=delay, db=db,
        )
        return "failed"
    scheduler.release(t_id, f"{result.status}: {result.detail}", retry_delay=delay, db=db)
    return "released"


def summarize_standalone_failures(version: str) -> None:
    """Standalone runs have no queue: log the steps of `version` that did not run or failed."""
    bad = {n: r for n, r in step_outcomes().items() if r.status in FOLLOWUP_STATUSES}
    if bad:
        logger.warning(COLOR.yellow(
            f"[Steps] '{version}' ingested without: "
            + "; ".join(f"{n} ({r.status}: {r.detail})" for n, r in bad.items())
            + ". Re-run the step (e.g. --resolve-ml, --preparse) once it is available."
        ))



def queue_followups_safely(task: Mapping[str, Any], commit_id: int, outcomes: Mapping[str, StepResult] | None = None) -> int:
    """`enqueue_step_followups()` for a just-ingested version task (its `commit_id` filled in) on a fresh
    connection; a queue error is logged and never keeps the version task from completing."""
    try:
        with G.DB() as db:
            return enqueue_step_followups(db, {**task, "commit_id": int(commit_id or task.get("commit_id") or 0)}, outcomes)
    except Exception as exc:  # noqa: BLE001 - the version itself is ingested; only the follow-ups are lost
        logger.error(f"[Steps] Could not queue follow-up tasks for '{task.get('ref_name')}': {exc}")
        return 0


def drain_aux_tasks(
    scheduler: Any,
    repo_path: str,
    repo_id: int | None,
    *,
    should_stop: Callable[[], bool],
    heartbeat_daemon: Any = None,
    max_tasks: int = 10000,
) -> int:
    """Claim and run, one by one, the auxiliary tasks `scheduler` (restricted to `AUX_TASK_TYPES`) may claim
    until none is claimable or `should_stop()`: the master's closing pass over the repository-level and follow-up
    tasks no capable worker took. Returns the number of tasks run."""
    from pipeline.git_sync import verify_local_ref_exists

    ran = 0
    while ran < max_tasks and not should_stop():
        with G.DB() as db:
            task = scheduler.claim_next(db)
        if task is None:
            break
        ran += 1
        t_id = int(task["task_id"])
        step = step_for_task_type(str(task.get("task_type") or ""))
        if step is None:
            with G.DB() as db:
                scheduler.release(t_id, "not an auxiliary task", db=db)
            break
        if step.scope == SCOPE_VERSION and not verify_local_ref_exists(repo_path, str(task.get("ref_name") or "")):
            with G.DB() as db:
                scheduler.release(t_id, "ref missing in local mirror", retry_delay=retry_delay(0), db=db)
            continue
        if heartbeat_daemon is not None:
            heartbeat_daemon.set_active(int(task.get("commit_id") or 0), dist_task_id=t_id)
        try:
            result = run_step_task(task, repo_path, repo_id)
            with G.DB() as db:
                action = finish_step_task(scheduler, task, result, db=db)
            logger.info(f"[Steps] Task {t_id} ({task.get('task_type')}: '{task.get('ref_name')}') {result.status} -> {action}.")
        finally:
            if heartbeat_daemon is not None:
                heartbeat_daemon.clear_active()
    return ran
