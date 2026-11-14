"""unit_test/runner.py - Test-suite runner behind `python3 main.py -u` (module list, isolation, summary).

`DEFAULT_MODULES` is the full suite. Plain `-u` runs it in-process (one `unittest` run). Any of `--summary`,
`--changed`, `--modules`, `--isolate`, `--timeout` runs every module in its own subprocess (sequentially, never
concurrently: DB tunnel port 3308) with a per-module timeout; a hung module is reported as an error.

Worker (internal): `python3 -m unit_test.runner --worker MODULE --out RESULT.json --log MODULE.log`.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TIMEOUT = 300
NO_LIVE_DB_ENV = "KIP_NO_LIVE_DB"

DEFAULT_MODULES = [
    "unit_test.test_db_engine",
    "unit_test.test_table_engine",
    "unit_test.test_table_handling",
    "unit_test.test_raw_parser",
    "unit_test.test_c_parser",
    "tests.test_maintainer_ast",
    "tests.test_maintainer_files",
    "tests.test_credits_lifecycle",
    "tests.test_tag_maps",
    "tests.test_git_commit_parser",
    "tests.test_tree_resolver",
    "tests.test_te_db_integrity",
    "tests.test_multicore_pipeline",
    "tests.test_pipeline_steps",
    "tests.test_workspace_checkout",
    "tests.test_commit_pipeline",
    "tests.test_symbol_rows",
    "tests.test_kconfig_ast",
    "tests.test_kconfig_engine",
    "tests.test_kbuild_conditions",
    "tests.test_arch_layout",
    "tests.test_include_graph",
    "tests.test_ingest_state",
    "tests.test_webapp_defconfig",
    "tests.test_webapp_maintainer",
    "tests.test_webapp_existing_data",
    "tests.test_person_identity",
    "tests.test_webapp_kbuild_usage",
    "tests.test_commit_relations",
    "tests.test_blob_fact_readers",
    "tests.test_webapp_file_references",
    "tests.test_raw_ast",
    "tests.test_rust_ast",
    "tests.test_config",
    "tests.test_logging_setup",
    "tests.test_ssh_tunnel",
    "tests.test_mailing_list",
    "tests.test_webapp_mail",
    "tests.test_git_reader",
    "tests.test_webapp_system",
    "tests.test_webapp_sync",
    "tests.test_file_references",
    "tests.test_duration_profiler",
    "tests.test_quit_listener",
    "tests.test_commit_diff",
    "tests.test_webapp_advanced_features",
    "tests.test_history_service",
    "tests.test_cast_golden",
    "tests.test_webapp_disk_cache",
    "tests.test_webapp_jobs",
    "tests.test_webapp_grep",
    "tests.test_webapp_blame",
    "tests.test_webapp_outline",
    "tests.test_webapp_rust_refs",
    "tests.test_webapp_release_index",
    "tests.test_webapp_commit_query",
    "tests.test_webapp_stable",
    "tests.test_webapp_insights",
    "tests.test_webapp_cve",
    "tests.test_cve_ingest",
    "tests.test_release_stats",
    "tests.test_webapp_kbuild_index",
    "tests.test_webapp_kapi",
    "tests.test_webapp_devices",
    "tests.test_webapp_patch_apply",
    "tests.test_webapp_patch_series",
    "tests.test_webapp_checkpatch",
    "tests.test_webapp_hover",
    "tests.test_webapp_source_index",
    "tests.test_facts_ingest",
    "tests.test_man_ast",
    "tests.test_webapp_man",
    "tests.test_kapi_facts",
    "tests.test_checkpoints",
    "tests.test_webapp_ops",
    "tests.test_webapp_callpath",
    "tests.test_webapp_pahole_compare",
    "tests.test_webapp_watch",
    "tests.test_webapp_theme_contrast",
    "tests.test_webapp_frontend_js",
    "tests.test_tui",
    "tests.test_tui_app",
    "tests.test_tui_testing",
    "tests.test_tui_output_capture",
    "tests.test_tui_reentry",
    "tests.test_tui_launch",
    "tests.test_tui_process_view",
    "tests.test_repos_screen",
    "tests.test_database_ssh_screen",
    "tests.test_parser_cve_screens",
    "tests.test_logging_screen",
    "tests.test_git_manager_screen",
    "tests.test_version_parse_screen",
    "tests.test_db_content",
    "tests.test_db_content_screen",
    "tests.test_git_select_screen",
    "tests.test_distributed_parsing",
    "tests.test_linux_branches_distributed",
    "tests.test_max_allowed_packet",
    "tests.test_resume_pipeline",
    "tests.test_te_chunk_dedup",
    "unit_test.test_sqlite_store",
    "tests.test_test_runner",
    # db_engine / cache_engine contract and rendering suites
    "unit_test.test_db_contract",
    "unit_test.test_db_spec_ext",
    "unit_test.test_ddl_render",
    "unit_test.test_cache_engine",
    # repository tooling: guard rules, code_symbol.py, generated docs (TOCs, API surfaces)
    "tests.test_code_symbol",
    "tests.test_pre_tool_guard",
    "tests.test_doc_toc",
    # architecture rules (no SQL / driver access outside db_engine and cache_engine)
    "tests.test_db_boundary",
    # The unittest classes of this module (libclang compatibility, parser cases) are separate from the multi-core
    # C-AST regression step of `main.py -u` (`tests.test_c_ast.run_c_ast_tests`), so it is listed here too.
    "tests.test_c_ast",
]

# `test_*.py` modules under tests/ and unit_test/ that are deliberately not part of the suite: {module: reason}.
# `tests/test_test_runner.py` fails for any test module that is neither in DEFAULT_MODULES nor listed here.
EXCLUDED_MODULES: dict[str, str] = {}


def normalize_module(name: str) -> str:
    """`tests/test_x.py`, `tests.test_x` and `test_x` (searched in `tests/` then `unit_test/`) -> dotted module name."""
    name = name.strip().replace("\\", "/")
    if name.endswith(".py"):
        name = name[:-3]
    name = name.replace("/", ".")
    if "." not in name:
        for pkg in ("tests", "unit_test"):
            if (ROOT / pkg / f"{name}.py").exists():
                return f"{pkg}.{name}"
    return name


def first_line(text: str) -> str:
    """Last non-empty line of a traceback (the exception message), trimmed."""
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    return (lines[-1] if lines else "")[:300]


def log_dir() -> Path:
    path = Path(os.environ.get("KIP_TEST_LOG_DIR") or Path(tempfile.gettempdir()) / "kip-unit-logs")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_affected():
    spec = importlib.util.spec_from_file_location("affected_tests", ROOT / ".agents" / "scripts" / "affected_tests.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def select_changed(base: str | None) -> tuple[list[str], bool, str | None]:
    """`(modules, full, why_full)` for the working tree vs `base` (default HEAD)."""
    at = _load_affected()
    result = at.affected(at.changed_files(base, ROOT), ROOT)
    return result["modules"], result["full"], result["why_full"]


# ---------------------------------------------------------------------------------------------- worker

def worker(module: str, out: str, log: str) -> int:
    """Run one module, write per-test results as JSON to `out` and the verbose unittest stream to `log`."""
    start = time.time()
    with open(log, "w", encoding="utf-8") as stream:
        suite = unittest.TestLoader().loadTestsFromName(module)
        res = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    rows = [("FAIL", t, tb) for t, tb in res.failures] + [("ERROR", t, tb) for t, tb in res.errors]
    data = {
        "module": module,
        "ran": res.testsRun,
        "failures": len(res.failures),
        "errors": len(res.errors),
        "skipped": len(res.skipped),
        "seconds": round(time.time() - start, 2),
        "problems": [{"kind": k, "id": t.id(), "message": first_line(tb), "traceback": tb} for k, t, tb in rows],
        "skips": sorted({reason for _, reason in res.skipped}),
    }
    Path(out).write_text(json.dumps(data), encoding="utf-8")
    return 0 if res.wasSuccessful() else 1


# --------------------------------------------------------------------------------------- isolated run

def run_module(module: str, timeout: int, work: Path, env: dict[str, str]) -> dict:
    """One module in a subprocess; returns the worker's result dict (or a synthetic one on timeout/crash)."""
    safe = module.replace(".", "_")
    out, log = work / f"{safe}.json", work / f"{safe}.log"
    cmd = [sys.executable, "-m", "unit_test.runner", "--worker", module, "--out", str(out), "--log", str(log)]
    start = time.time()
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            start_new_session=True)
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        output, _ = proc.communicate()
        return _synthetic(module, f"TIMEOUT after {timeout}s (module hung or too slow)", output, start, log)
    if out.exists():
        data = json.loads(out.read_text(encoding="utf-8"))
        data["log"] = str(log)
        data["output"] = output
        return data
    return _synthetic(module, f"module crashed (exit {proc.returncode}) before reporting", output, start, log)


def _synthetic(module: str, message: str, output: str, start: float, log: Path) -> dict:
    tail = "\n".join((output or "").strip().splitlines()[-30:])
    return {
        "module": module, "ran": 0, "failures": 0, "errors": 1, "skipped": 0, "seconds": round(time.time() - start, 2),
        "problems": [{"kind": "ERROR", "id": module, "message": message, "traceback": f"{message}\n{tail}"}],
        "skips": [], "log": str(log), "output": output, "timeout": message.startswith("TIMEOUT"),
    }


def run_isolated(modules: list[str], *, timeout: int = DEFAULT_TIMEOUT, summary: bool = False,
                 no_live_db: bool = False, out=sys.stdout) -> int:
    """Run `modules` one subprocess at a time; print per-module lines (or the compact summary); return exit code."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = log_dir() / f"unit-{stamp}-{os.getpid()}"
    work.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if no_live_db:
        env[NO_LIVE_DB_ENV] = "1"
    results: list[dict] = []
    wall = time.time()
    for module in modules:
        res = run_module(module, timeout, work, env)
        results.append(res)
        if not summary:
            status = "OK" if not (res["failures"] or res["errors"]) else "FAILED"
            print(f"[{status}] {module}: ran {res['ran']} in {res['seconds']}s "
                  f"(F={res['failures']} E={res['errors']} S={res['skipped']})", file=out, flush=True)
    wall = time.time() - wall

    full_log = work / "full.log"
    with open(full_log, "w", encoding="utf-8") as fh:
        for res in results:
            fh.write(f"===== {res['module']} (ran {res['ran']}, {res['seconds']}s) =====\n")
            for p in res["problems"]:
                fh.write(f"--- {p['kind']} {p['id']}\n{p['traceback']}\n")
            fh.write(Path(res["log"]).read_text(encoding="utf-8") if Path(res["log"]).exists() else "")
            fh.write(res.get("output") or "")
    totals = {k: sum(r[k] for r in results) for k in ("ran", "failures", "errors", "skipped")}
    problems = [(r["module"], p) for r in results for p in r["problems"]]
    for module, p in problems:
        print(f"{p['kind']} {p['id']}: {p['message']}", file=out)
    skip_reasons = sorted({s for r in results for s in r["skips"]})
    if summary and skip_reasons:
        for reason in skip_reasons[:5]:
            print(f"SKIP reason: {reason[:200]}", file=out)
    timeouts = sum(1 for r in results if r.get("timeout"))
    print(f"ran={totals['ran']} failures={totals['failures']} errors={totals['errors']} skipped={totals['skipped']} "
          f"time={wall:.1f}s modules={len(results)} timeouts={timeouts}", file=out)
    print(f"full log: {full_log}", file=out, flush=True)
    return 0 if not (totals["failures"] or totals["errors"]) else 1


def run_in_process(modules: list[str], verbosity: int = 2):
    """Today's `-u` behaviour: all modules in one `unittest` run in this process."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for mod_name in modules:
        try:
            suite.addTests(loader.loadTestsFromName(mod_name))
        except Exception as e:  # noqa: BLE001
            print(f"Failed loading test module {mod_name}: {e}", file=sys.stderr)
    return unittest.TextTestRunner(verbosity=verbosity).run(suite)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m unit_test.runner")
    ap.add_argument("--worker", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--log", required=True)
    args = ap.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    return worker(args.worker, args.out, args.log)


if __name__ == "__main__":
    sys.exit(main())
