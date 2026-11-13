"""Parse pipeline of `main.py`, one module per responsibility (each below 40 KB).

Process-wide state (`G`, `MF`, `gp`, `logger`) lives in `pipeline/state.py`; importing it performs the import-time
setup. Modules import the names they call from the module that defines them (no re-exports), so tests patch a name
on the module that looks it up.

- `state`: process-wide objects. `db_setup`: startup migrations, indexes, views.
- `cli`: `arg_handling`. `repair`: `orchestrate_repair_pipeline`. `update`: `update()` per-version sequence.
- Parallel parsing: `worker_protocol` (frames), `worker_stage` (pool stage), `systemic` (version-wide failures),
  `multicore` (`trigger_multicore`, DB tuning), `scheduler` (dependency-driven ChangeSet resolution),
  `source_order` (changed-file ordering and epochs), `file_processing` (worker body), `changesets`, `blob_reuse`.
- Git and per-version steps: `git_network`, `git_ingest`, `git_commit_worker`, `commits`, `mailing_list_steps`,
  `unchanged`, `file_references`, `maintainers`, `arch_layouts`, `external_steps` (source facts, CVE, release
  statistics), `backfill_steps`.
"""
