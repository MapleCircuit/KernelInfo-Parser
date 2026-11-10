"""parser/cve/cve_ingest.py - Ingest a local CVE list into the database (`m_cve_source`, `m_cve`, `m_cve_commit`).

The list is a Git clone the operator keeps current (`git pull`), named by config `cve.repo_dir` (default
`cvelistV5`, relative to the project root). Two layouts are read:

- the CVE Program's list (`cvelistV5`: every CNA, `cves/<year>/<n>xxx/CVE-*.json`, ~420k records): the kernel's
  records are picked with one `git grep -l` over HEAD (kernel CNA assigner, kernel commit URLs, "Linux kernel"),
  then classified (`cve_records.parse_record`);
- the kernel CNA's own repository (`vulns.git`: `cve/published/<year>/CVE-*.json`, all kernel records).

Records are read through `git cat-file --batch` (paths come from Git listings; a record over 1 MB is skipped).
`ingest()` compares the clone's HEAD with the one `m_cve_source` recorded: equal, nothing is done; a descendant
of it, only the records `git diff --name-only` names are read again (~0.2 s); otherwise (first run, another
list, a new `RECORD_FORMAT`) the whole list is ingested again (~15 s). Each ingestion is one transaction, so the
webapp always reads one complete version of the list. `main.py` runs it at the start of every parser run and
alone with `--cve` (`core/API.md` section 9).
"""
from __future__ import annotations

import contextlib
import json
import logging
import re
import subprocess
import time
import zlib
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from parser.cve.cve_records import (
    KINDS,
    RECORD_FORMAT,
    may_be_kernel,
    parse_record,
    record_commits,
    GREP_MARKERS,
)

logger = logging.getLogger(__name__)


MAX_RECORD = 1 << 20
LAYOUTS = {"cvelist": "cves", "vulns": "cve/published"}
_FULL = re.compile(r"^[0-9a-f]{40}$")
_ID_FROM_PATH = re.compile(r"(CVE-\d{4}-\d{4,})\.json$")
_BATCH = 500


# ------------------------------------------------------------------------------------- the clone

def repo_dir(raw: str | None = None) -> Path | None:
    """The configured clone (`cve.repo_dir`, relative to the project root) when it is a git repository."""
    try:
        from core.config import REPO_ROOT, get_config
        if raw is None:
            raw = str(((get_config().get("cve") or {}).get("repo_dir")) or "")
    except Exception:  # noqa: BLE001 - no configuration: off
        return None
    raw = str(raw or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    path = path.resolve()
    if not path.is_dir() or not ((path / ".git").exists() or (path / "HEAD").is_file()):
        return None
    return path


def _git(repo: Path, args: list[str], timeout: float = 60.0) -> bytes | None:
    try:
        out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def head_of(repo: Path) -> str | None:
    out = (_git(repo, ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], timeout=10) or b"").decode().strip()
    return out if _FULL.match(out) else None


def detect_layout(repo: Path, head: str) -> str | None:
    """"cvelist" (cves/) or "vulns" (cve/published/) from the top of the clone's tree."""
    names = set((_git(repo, ["ls-tree", "--name-only", head], timeout=30) or b"").decode("utf-8", "replace").split("\n"))
    if "cves" in names:
        return "cvelist"
    if "cve" in names:
        return "vulns"
    return None


def candidate_paths(repo: Path, head: str, layout: str) -> list[str]:
    """Record paths that may concern the kernel (every record of vulns.git)."""
    root = LAYOUTS[layout]
    if layout == "vulns":
        out = _git(repo, ["ls-tree", "-r", "-z", "--name-only", head, "--", root], timeout=300) or b""
        return [p.decode("utf-8", "replace") for p in out.split(b"\0") if p.endswith(b".json") and b"/CVE-" in p]
    args = ["grep", "-l", "-z", "-i", "-F"]
    for marker in GREP_MARKERS:
        args += ["-e", marker]
    out = _git(repo, [*args, head, "--", root], timeout=900) or b""
    prefix = f"{head}:".encode()
    paths = []
    for p in out.split(b"\0"):
        if p.startswith(prefix):
            p = p[len(prefix):]
        if p.endswith(b".json") and b"/CVE-" in p:
            paths.append(p.decode("utf-8", "replace"))
    return paths


def changed_paths(repo: Path, old: str, new: str, root: str) -> list[str] | None:
    """Record paths changed between two heads (None: `old` is unknown here, e.g. the clone was replaced)."""
    out = _git(repo, ["diff", "--name-only", "-z", "--no-renames", old, new, "--", root], timeout=300)
    if out is None:
        return None
    return [p.decode("utf-8", "replace") for p in out.split(b"\0") if p.endswith(b".json") and b"/CVE-" in p]


def cat_file(repo: Path, head: str, paths: list[str], progress: Callable | None = None) -> Iterator[tuple[str, bytes | None]]:
    """(path, content) of each path at `head` through one `git cat-file --batch` (None: missing or too big)."""
    import threading
    proc = subprocess.Popen(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        bufsize=1024 * 1024,
    )

    def _feeder():
        try:
            buf = []
            for path in paths:
                if "\n" in path or "\0" in path:
                    buf.append(b"__invalid_ref__\n")
                else:
                    buf.append(f"{head}:{path}\n".encode())
                if len(buf) >= 128:
                    proc.stdin.write(b"".join(buf))
                    buf.clear()
            if buf:
                proc.stdin.write(b"".join(buf))
                buf.clear()
            proc.stdin.flush()
        except (OSError, BrokenPipeError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass

    feeder_thread = threading.Thread(target=_feeder, daemon=True)
    feeder_thread.start()

    try:
        for i, path in enumerate(paths):
            if "\n" in path or "\0" in path:
                # Discard response for dummy ref
                proc.stdout.readline()
                yield path, None
                continue
            line = proc.stdout.readline()
            if not line:
                yield path, None
                continue
            header = line.split()
            if len(header) < 3 or header[1] != b"blob":
                yield path, None
                continue
            size = int(header[2])
            body = proc.stdout.read(size)
            proc.stdout.read(1)
            yield path, (body if size <= MAX_RECORD else None)
            if progress and i % 2000 == 0:
                progress(i, len(paths))
    finally:
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        feeder_thread.join(timeout=5)


def read_records(repo: Path, head: str, layout: str, paths: list[str],
                 progress: Callable | None = None) -> tuple[dict[str, tuple[str, dict[str, Any]]], dict[str, int]]:
    """{cve id: (path, parsed record)} of the kernel records among `paths`, and counts (read, skipped, unparsed)."""
    records: dict[str, tuple[str, dict[str, Any]]] = {}
    counts = {"read": 0, "skipped": 0, "unparsed": 0}
    for path, body in cat_file(repo, head, paths, progress):
        counts["read"] += 1
        if body is None:
            counts["skipped"] += 1
            continue
        if layout == "cvelist" and not may_be_kernel(body):
            continue
        try:
            rec = parse_record(json.loads(body))
        except (ValueError, TypeError, AttributeError):
            counts["unparsed"] += 1
            continue
        if rec:
            records[rec["id"]] = (path, rec)
    return records, counts


# ------------------------------------------------------------------------------------- rows

def _epoch(date: str) -> int:
    try:
        return int(datetime.strptime(str(date)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return 0


def record_row(path: str, rec: dict[str, Any]) -> tuple[Any, ...]:
    """The m_cve row of a parsed record."""
    cvss = rec.get("cvss") or {}
    try:
        score = max(0, min(100, int(round(float(cvss.get("score") or 0) * 10))))
    except (TypeError, ValueError):
        score = 0
    data = zlib.compress(json.dumps(rec, separators=(",", ":"), sort_keys=True).encode("utf-8"), 6)
    return (rec["id"], KINDS.get(rec.get("kind"), 3), _epoch(rec.get("published") or ""), _epoch(rec.get("updated") or ""),
            score, 1 if rec.get("kev") else 0, path[:255], str(rec.get("title") or "")[:512], data)


def commit_rows(rec: dict[str, Any]) -> list[tuple[str, int, str]]:
    return [(sha, role, rec["id"]) for sha, role in record_commits(rec)]


def source_state(db: Any) -> dict[str, Any] | None:
    """The list the database holds (one row), or None."""
    row = db.query_one("SELECT name, layout, head, format, records, built_at FROM m_cve_source ORDER BY built_at DESC LIMIT 1", dictionary=False)
    if not row:
        return None
    name, layout, head, fmt, records, built_at = row
    name = name.decode() if isinstance(name, (bytes, bytearray)) else name
    layout = layout.decode() if isinstance(layout, (bytes, bytearray)) else layout
    return {"name": name, "layout": layout, "head": bytes(head).hex() if head is not None else None,
            "format": int(fmt), "records": int(records), "built_at": int(built_at)}


def _ids_of(paths: Iterable[str]) -> list[str]:
    return sorted({m.group(1) for p in paths if (m := _ID_FROM_PATH.search(p))})


def _delete_ids(db: Any, ids: list[str]) -> None:
    for i in range(0, len(ids), _BATCH):
        part = ids[i:i + _BATCH]
        marks = ",".join(["%s"] * len(part))
        db.execute(f"DELETE FROM m_cve_file WHERE cve_id IN ({marks})", part)  # noqa: S608
        db.execute(f"DELETE FROM m_cve_commit WHERE cve_id IN ({marks})", part)  # noqa: S608
        db.execute(f"DELETE FROM m_cve WHERE cve_id IN ({marks})", part)  # noqa: S608


def _insert(db: Any, records: dict[str, tuple[str, dict[str, Any]]]) -> None:
    rows = [record_row(path, rec) for path, rec in records.values()]
    links = [link for _path, rec in records.values() for link in commit_rows(rec)]
    file_rows = [
        (rec["id"], f_path[:255])
        for _path, rec in records.values()
        for f_path in (rec.get("files") or ())
        if f_path
    ]
    for i in range(0, len(rows), _BATCH):
        db.executemany(
            "INSERT INTO m_cve (cve_id, kind, published, updated, score, kev, record_path, title, data) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE kind = VALUES(kind), "
            "published = VALUES(published), updated = VALUES(updated), score = VALUES(score), kev = VALUES(kev), "
            "record_path = VALUES(record_path), title = VALUES(title), data = VALUES(data)",
            rows[i:i + _BATCH],
        )
    for i in range(0, len(links), 2000):
        db.executemany(
            "INSERT IGNORE INTO m_cve_commit (commit_hash, role, cve_id) VALUES (%s, %s, %s)",
            links[i:i + 2000],
        )
    for i in range(0, len(file_rows), 2000):
        db.executemany(
            "INSERT IGNORE INTO m_cve_file (cve_id, file_path) VALUES (%s, %s)",
            file_rows[i:i + 2000],
        )


# ------------------------------------------------------------------------------------- ingestion

def prepare(repo: Path, prev: dict[str, Any] | None, force: bool = False, progress: Callable | None = None) -> dict[str, Any]:
    """What an ingestion of the clone's HEAD has to write, given the list the database holds (`source_state()`).

    {state: "missing" | "current" | "full" | "incremental", head, layout, name, paths, records, counts}: `records`
    are the kernel records among `paths` (every candidate for "full", the changed records for "incremental")."""
    head = head_of(repo)
    layout = detect_layout(repo, head) if head else None
    if head is None or layout is None:
        return {"state": "missing"}
    name = repo.name[:64]
    same_list = bool(prev) and prev["name"] == name and prev["layout"] == layout and prev["format"] == RECORD_FORMAT
    if same_list and prev["head"] == head and not force:
        return {"state": "current", "head": head, "layout": layout, "name": name, "records_total": prev["records"]}
    paths: list[str] | None = None
    if same_list and not force and prev["head"]:
        paths = changed_paths(repo, prev["head"], head, LAYOUTS[layout])
    mode = "incremental" if paths is not None else "full"
    if paths is None:
        paths = candidate_paths(repo, head, layout)
    records, counts = read_records(repo, head, layout, paths, progress)
    return {"state": mode, "head": head, "layout": layout, "name": name, "paths": paths, "records": records, "counts": counts}


def write(db: Any, plan: dict[str, Any]) -> int:
    """Apply a `prepare()` plan in one transaction; returns the records the database then holds."""
    try:
        if plan["state"] == "full":
            db.execute("DELETE FROM m_cve_file")
            db.execute("DELETE FROM m_cve_commit_hash")
            db.execute("DELETE FROM m_cve_commit")
            db.execute("DELETE FROM m_cve")
            db.execute("DELETE FROM m_cve_source")
        else:
            # A changed record is replaced; one no longer about the kernel (or deleted) goes away
            _delete_ids(db, _ids_of(plan["paths"]))
        _insert(db, plan["records"])
        cnt_row = db.query_one("SELECT COUNT(*) FROM m_cve", dictionary=False)
        total = int(cnt_row[0]) if cnt_row else 0
        db.execute(
            "INSERT INTO m_cve_source (name, layout, head, format, records, built_at) VALUES (%s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE layout = VALUES(layout), head = VALUES(head), format = VALUES(format), "
            "records = VALUES(records), built_at = VALUES(built_at)",
            (plan["name"], plan["layout"], bytes.fromhex(plan["head"]), RECORD_FORMAT, total, int(time.time())),
        )
        db.commit()
    except Exception:
        if hasattr(db, "rollback"):
            db.rollback()
        raise
    return total


def resolve_commit_prefixes(db: Any, kernel_repo: Path | str, repo_id: int) -> dict[str, int]:
    """Store in m_cve_commit_hash the commit each abbreviated m_cve_commit hash names in the kernel repository
    (`commit_relations.resolve_prefixes`), for the prefixes without a row yet; a prefix naming no commit or
    several gets none and is tried again next time. Returns {pending, resolved}."""
    from parser.git_ast.commit_relations import resolve_prefixes
    rows = db.query(
        "SELECT DISTINCT c.commit_hash FROM m_cve_commit c WHERE CHAR_LENGTH(c.commit_hash) < 40 "
        "AND NOT EXISTS (SELECT 1 FROM m_cve_commit_hash h WHERE h.repo_id = %s AND h.prefix = c.commit_hash)",
        (repo_id,),
        dictionary=False,
    )
    pending = [r[0].decode() if isinstance(r[0], (bytes, bytearray)) else str(r[0]) for r in rows]
    found = resolve_prefixes(kernel_repo, pending) if pending else {}
    rows_to_ins = [(repo_id, p, bytes.fromhex(found[p.lower()])) for p in pending if p.lower() in found]
    for i in range(0, len(rows_to_ins), 2000):
        db.executemany("INSERT IGNORE INTO m_cve_commit_hash (repo_id, prefix, commit_hash) VALUES (%s, %s, %s)", rows_to_ins[i:i + 2000])
    db.commit()
    if pending:
        logger.info(f"[CVE] Resolved {len(rows_to_ins)} of {len(pending)} abbreviated commit hash(es) in {kernel_repo}.")
    return {"pending": len(pending), "resolved": len(rows_to_ins)}


def ingest(db: Any, repo: Path | None = None, force: bool = False, progress: Callable | None = None) -> dict[str, Any]:
    """Bring the database's CVE tables to the clone's HEAD. Returns a summary
    ({state: "missing" | "current" | "incremental" | "full", records, changed, seconds, ...})."""
    repo = repo or repo_dir()
    if repo is None or db is None:
        return {"state": "missing"}
    started = time.time()
    prev = source_state(db)
    plan = prepare(repo, prev, force, progress)
    if plan["state"] == "missing":
        logger.warning(f"[CVE] {repo} holds neither cves/ (cvelistV5) nor cve/published/ (vulns.git); skipped.")
        return plan
    if plan["state"] == "current":
        return {"state": "current", "head": plan["head"], "records": plan["records_total"]}
    total = write(db, plan)
    summary = {"state": plan["state"], "head": plan["head"], "layout": plan["layout"], "records": total,
               "changed": len(plan["records"]), "paths": len(plan["paths"]), "seconds": round(time.time() - started, 1),
               **plan["counts"]}
    logger.info(
        f"[CVE] {plan['name']} at {plan['head'][:12]} ({plan['state']}): {len(plan['records'])} kernel record(s) written "
        f"from {len(plan['paths'])} path(s), {total} in the database, {summary['seconds']}s."
    )
    return summary
