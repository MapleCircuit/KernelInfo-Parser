"""core/backfills.py - One-off refills of derived tables for versions ingested before they existed.

Rules (plans/database/README.md rule 5):
- a backfill never blocks parsing: callers log its failure and go on;
- it is skipped while a version ingestion is interrupted (`INGEST_PARSING`), since that version's rollback
  deletes rows a backfill would reference;
- it is resumable (idempotent primary keys, or "rows still missing" as the state) and logs progress every
  20 seconds;
- in distributed mode it runs on the master (or standalone) only.

Each backfill takes a database driver (`db`) and returns a summary dict with a `state` key:
`done`, `deferred` (an ingestion is interrupted) or `unavailable` (no SQL connection).
"""
from __future__ import annotations

import contextlib
import logging
import time
from typing import Any, Callable, Iterable

from core import ingest_state

logger = logging.getLogger(__name__)

PROGRESS_EVERY = 20.0
INSERT_CHUNK = 5000


class Progress:
    """Logs `label: done/total` at most every PROGRESS_EVERY seconds."""

    def __init__(self, label: str, total: int = 0) -> None:
        self.label = label
        self.total = total
        self.done = 0
        self._last = time.monotonic()
        self.started = self._last

    def step(self, n: int = 1, note: str = "") -> None:
        self.done += n
        now = time.monotonic()
        if now - self._last >= PROGRESS_EVERY:
            self._last = now
            total = f"/{self.total}" if self.total else ""
            logger.info(f"[{self.label}] {self.done}{total} {note}".rstrip())

    @property
    def seconds(self) -> float:
        return round(time.monotonic() - self.started, 1)


def _query_rows(db: Any, sql: str, params: tuple = ()) -> list[tuple]:
    if hasattr(db, "query") and callable(db.query):
        res = db.query(sql, params, dictionary=False)
        if isinstance(res, list):
            return res
    if hasattr(db, "execute") and callable(db.execute):
        res = db.execute(sql, params)
        if isinstance(res, list):
            return res
    return []


def _query_one_row(db: Any, sql: str, params: tuple = ()) -> tuple | None:
    if hasattr(db, "query_one") and callable(db.query_one):
        res = db.query_one(sql, params, dictionary=False)
        if isinstance(res, (tuple, list)):
            return tuple(res)
        if res is None:
            return None
    if hasattr(db, "execute") and callable(db.execute):
        res = db.execute(sql, params)
        if isinstance(res, (tuple, list)):
            return tuple(res)
    return None


def interrupted_ingests(db: Any) -> int:
    """Number of versions whose ingestion is interrupted (m_ingest_state INGEST_PARSING).
    In distributed mode, versions actively being parsed by a heartbeating node are in progress, not interrupted.
    """
    now = int(time.time())
    try:
        row = _query_one_row(
            db,
            """
            SELECT COUNT(*)
            FROM m_ingest_state s
            LEFT JOIN m_parse_node n ON n.node_id = s.worker_id
            WHERE s.status = %s
            AND (s.worker_id = '' OR n.node_id IS NULL OR n.heartbeat_at < %s)
            """,
            (ingest_state.INGEST_PARSING, now - 180),
        )
        return int((row or (0,))[0] or 0)
    except Exception:
        row = _query_one_row(db, "SELECT COUNT(*) FROM m_ingest_state WHERE status = %s", (ingest_state.INGEST_PARSING,))
        return int((row or (0,))[0] or 0)


def guarded(label: str, db: Any, run: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run a backfill under the rules above: deferred while an ingestion is interrupted, never raising."""
    if db is None:
        return {"state": "unavailable"}
    try:
        interrupted = interrupted_ingests(db)
        if interrupted:
            logger.info(f"[{label}] Skipped: {interrupted} version ingestion(s) interrupted; run it again once recovered.")
            return {"state": "deferred"}
        return run()
    except Exception as exc:  # noqa: BLE001 - a backfill never blocks parsing
        logger.warning(f"[{label}] Failed: {exc}", exc_info=True)
        if hasattr(db, "rollback") and callable(db.rollback):
            with contextlib.suppress(Exception):
                db.rollback()
        return {"state": "error", "error": str(exc)}


def finished_versions(db: Any, network_id: int, repo_id: int) -> list[tuple[str, int, int]]:
    """(ref_name, commit_id, base_commit_id) of the repository's finished versions, in ingestion order."""
    rows = _query_rows(
        db,
        """
        SELECT r.ref_name, s.commit_id, s.base_commit_id
        FROM m_ingest_state s
        JOIN m_git_ref r ON r.commit_id = s.commit_id AND r.repo_id = %s
        WHERE s.network_id = %s AND s.status = %s
        ORDER BY s.updated_at, s.commit_id
        """,
        (repo_id, network_id, ingest_state.INGEST_DONE),
    )
    out: list[tuple[str, int, int]] = []
    seen: set[int] = set()
    for name, cid, base in rows:
        if int(cid) in seen:
            continue
        seen.add(int(cid))
        if isinstance(name, (bytes, bytearray)):
            name = name.decode("utf-8", "replace")
        out.append((str(name), int(cid), int(base or 0)))
    return out


def fnids_of(db: Any, paths: Iterable[str], cache: dict[str, int]) -> dict[str, int]:
    """path -> m_file_name.fnid for the paths that have one (filled into cache)."""
    missing = sorted({p for p in paths if p not in cache})
    for i in range(0, len(missing), 1000):
        chunk = missing[i : i + 1000]
        sql = f"SELECT fnid, fname FROM m_file_name WHERE fname IN ({','.join(['%s'] * len(chunk))})"
        rows = _query_rows(db, sql, tuple(chunk))
        for fnid, fname in rows:
            if isinstance(fname, (bytes, bytearray)):
                fname = fname.decode("utf-8", "replace")
            cache[fname] = int(fnid)
    return {p: cache[p] for p in paths if p in cache}


def insert_ignore(db: Any, table: str, columns: tuple[str, ...], rows: list[tuple[Any, ...]]) -> int:
    """INSERT IGNORE rows into table in chunks; returns the number of rows sent."""
    if not rows:
        return 0
    one = "(" + ",".join(["%s"] * len(columns)) + ")"
    for i in range(0, len(rows), INSERT_CHUNK):
        chunk = rows[i : i + INSERT_CHUNK]
        db.execute(
            f"INSERT IGNORE INTO {table} ({','.join(columns)}) VALUES {','.join([one] * len(chunk))}",
            tuple(v for row in chunk for v in row),
        )
    return len(rows)


# -----------------------------------------------------------------------------
# D01: m_maintainer_file (--backfill-maintainer-files [--from <ref>])
# -----------------------------------------------------------------------------

def backfill_maintainer_files(db: Any, mf: Any, network_id: int, repo_id: int, from_ref: str | None = None) -> dict[str, Any]:
    """Refill m_maintainer_file for the finished versions of the repository, as Step 6.7 would have
    (parser/maintainer_ast/maintainer_store.map_files): one commit per version, `from_ref` resumes."""

    def run() -> dict[str, Any]:
        from parser.maintainer_ast import maintainer_store
        from parser.maintainer_ast.maintainer_parser import MaintainerParser

        versions = finished_versions(db, network_id, repo_id)
        names = {cid: name for name, cid, _base in versions}
        if from_ref:
            idx = next((i for i, v in enumerate(versions) if v[0] == from_ref), None)
            if idx is None:
                raise ValueError(f"--from {from_ref}: not a finished version of this repository")
            versions = versions[idx:]
        progress = Progress("Backfill maintainer files", len(versions))
        fnid_cache: dict[str, int] = {}
        rows_total = skipped = 0
        for ref, cid, base in versions:
            blob = maintainer_store.version_blob(db, cid, "MAINTAINERS")
            cur_ids = maintainer_store.blob_section_ids(db, blob) if blob else {}
            if not cur_ids:
                skipped += 1
                progress.step(note=ref)
                continue
            prev_ids: set[int] = set()
            base_name = names.get(base)
            if base:
                prev_blob = maintainer_store.version_blob(db, base, "MAINTAINERS")
                prev_ids = set(maintainer_store.blob_section_ids(db, prev_blob).values()) if prev_blob else set()
            mapped = maintainer_store.sections_with_rows(db, prev_ids) if prev_ids else set()
            changed: set[int] | None = (set(cur_ids.values()) - prev_ids) if (mapped and base_name) else None
            all_paths = (mf.git_file_list(ref) or "").splitlines()
            new_paths: list[str] = []
            if changed is not None:
                old = set((mf.git_file_list(base_name) or "").splitlines())
                new_paths = [p for p in all_paths if p not in old]
                if not changed and not new_paths:
                    progress.step(note=ref)
                    continue
            sections = MaintainerParser(mf.get_file("MAINTAINERS", ref) or "").parse()
            pairs = maintainer_store.map_files(sections, cur_ids, changed, all_paths, new_paths)
            fnids = fnids_of(db, {p for _s, p in pairs}, fnid_cache)
            rows = sorted({(sid, fnids[p]) for sid, p in pairs if p in fnids})
            rows_total += insert_ignore(db, "m_maintainer_file", ("sec_id", "fnid"), rows)
            if hasattr(db, "commit") and callable(db.commit):
                db.commit()
            progress.step(note=f"{ref} ({len(rows)} rows)")
        logger.info(f"[Backfill maintainer files] {len(versions)} version(s), {rows_total} row(s) sent in {progress.seconds}s.")
        return {"state": "done", "versions": len(versions), "rows": rows_total, "skipped": skipped, "seconds": progress.seconds}

    return guarded("Backfill maintainer files", db, run)


# -----------------------------------------------------------------------------
# B10: version-diff m_commit_file rows on version head commits (--repair-head-commit-files)
# -----------------------------------------------------------------------------

def _diff_tree_files(repo_dir: str, shas: list[str]) -> dict[str, set[str]]:
    """Paths each commit changed against its first parent (`git diff-tree --stdin`, renames as delete + add)."""
    import subprocess
    out = subprocess.run(
        ["git", "-C", repo_dir, "diff-tree", "--stdin", "--always", "-r", "--name-only", "--root", "--no-renames"],
        input="".join(f"{s}\n" for s in shas).encode(),
        capture_output=True,
        timeout=600,
        check=True,
    ).stdout.decode("utf-8", "replace")
    files: dict[str, set[str]] = {}
    current: set[str] | None = None
    wanted = set(shas)
    for line in out.splitlines():
        if line in wanted:
            current = files.setdefault(line, set())
        elif line and current is not None:
            current.add(line)
    return files


def repair_head_commit_files(db: Any, repo_dir: str, network_id: int) -> dict[str, Any]:
    """Delete the m_commit_file rows of version head commits that name files the commit did not touch.

    Step 4.5 wrote one such row per reused blob of the version diff before 2026-11-07; the rows a head
    commit really has (Step 6.5) are kept. A merge head keeps none (merges have no m_commit_file rows)."""

    def run() -> dict[str, Any]:
        rows = _query_rows(
            db,
            """
            SELECT s.commit_id, c.commit_hash,
                   (SELECT COUNT(*) FROM m_commit_parent p WHERE p.commit_id = s.commit_id) AS parents
            FROM m_ingest_state s
            JOIN m_commit c ON c.commit_id = s.commit_id
            WHERE s.network_id = %s AND s.status = %s
              AND EXISTS (SELECT 1 FROM m_commit_file cf WHERE cf.commit_id = s.commit_id)
            ORDER BY s.commit_id
            """,
            (network_id, ingest_state.INGEST_DONE),
        )
        heads = []
        for cid, chash, parents in rows:
            sha = bytes(chash).hex() if isinstance(chash, (bytes, bytearray, memoryview)) else str(chash)
            heads.append((int(cid), sha, int(parents or 0)))
        progress = Progress("Repair head commit files", len(heads))
        deleted = 0
        for i in range(0, len(heads), 200):
            batch = heads[i : i + 200]
            touched = _diff_tree_files(repo_dir, [sha for _cid, sha, parents in batch if parents < 2])
            for cid, sha, parents in batch:
                cf_rows = _query_rows(
                    db,
                    "SELECT cf.fnid, fn.fname FROM m_commit_file cf JOIN m_file_name fn ON fn.fnid = cf.fnid WHERE cf.commit_id = %s",
                    (cid,),
                )
                real = touched.get(sha, set()) if parents < 2 else set()
                stale = []
                for fnid, fname in cf_rows:
                    if isinstance(fname, (bytes, bytearray)):
                        fname = fname.decode("utf-8", "replace")
                    if fname not in real:
                        stale.append(int(fnid))
                for j in range(0, len(stale), 1000):
                    chunk = stale[j : j + 1000]
                    db.execute(
                        f"DELETE FROM m_commit_file WHERE commit_id = %s AND fnid IN ({','.join(['%s'] * len(chunk))})",
                        (cid, *chunk),
                    )
                deleted += len(stale)
                progress.step()
            if hasattr(db, "commit") and callable(db.commit):
                db.commit()
        logger.info(f"[Repair head commit files] {len(heads)} head commit(s), {deleted} row(s) deleted in {progress.seconds}s.")
        return {"state": "done", "heads": len(heads), "deleted": deleted, "seconds": progress.seconds}

    return guarded("Repair head commit files", db, run)


# -----------------------------------------------------------------------------
# D06: m_kbuild_symbol (--backfill-kbuild; automatic once when the table is created)
# -----------------------------------------------------------------------------

def delete_old_kbuild_references(db: Any, chunk: int = 50_000) -> int:
    """Delete the Kbuild / Makefile rows (ref_type 3 / 4) m_file_reference held before m_kbuild_symbol;
    returns the number deleted."""
    deleted = 0
    while True:
        n = db.execute("DELETE FROM m_file_reference WHERE ref_type IN (3, 4) LIMIT %s", (chunk,))
        n = int(n or 0)
        if hasattr(db, "commit") and callable(db.commit):
            db.commit()
        deleted += n
        if n < chunk:
            break
    if deleted:
        logger.info(f"[Kbuild] Deleted {deleted} Kbuild/Makefile rows of m_file_reference (now in m_kbuild_symbol).")
    return deleted


def _kbuild_pairs(db: Any) -> tuple[list[tuple[int, int]], dict[int, str], dict[str, int]]:
    """((fnid, blob_id) of every Makefile/Kbuild path and blob a finished version's checkpoint holds,
    fnid -> path, path -> fnid).

    Each checkpoint's rows are read by primary key (commit_id, fnid) restricted to the Makefile paths: a
    checkpoint stored as a delta holds the pairs new since its base, its base the others, so the union
    over every version is every pair."""
    from parser.kbuild_symbols import is_kbuild_file
    fn_rows = _query_rows(db, "SELECT fnid, fname FROM m_file_name WHERE fnid > 0")
    path_of: dict[int, str] = {}
    fnid_of: dict[str, int] = {}
    for fnid, fname in fn_rows:
        if isinstance(fname, (bytes, bytearray)):
            fname = fname.decode("utf-8", "replace")
        fnid_of[fname] = int(fnid)
        if is_kbuild_file(fname):
            path_of[int(fnid)] = fname
    fnids = sorted(path_of)
    v_rows = _query_rows(db, "SELECT DISTINCT commit_id FROM m_ingest_state WHERE status = %s", (ingest_state.INGEST_DONE,))
    versions = sorted(int(r[0]) for r in v_rows)
    pairs: set[tuple[int, int]] = set()
    for cid in versions:
        for i in range(0, len(fnids), 2000):
            chunk = fnids[i:i + 2000]
            sql_bp = f"SELECT fnid, blob_id FROM m_bridge_checkpoint WHERE commit_id = %s AND fnid IN ({','.join(['%s'] * len(chunk))}) AND blob_id > 0"
            bp_rows = _query_rows(db, sql_bp, (cid, *chunk))
            pairs.update((int(f), int(b)) for f, b in bp_rows)
    return sorted(pairs), path_of, fnid_of


def backfill_kbuild(db: Any, mf: Any) -> dict[str, Any]:
    """Fill m_kbuild_symbol for the Makefile/Kbuild (path, blob) pairs of the database that have no row:
    each blob read from Git by SHA, parsed against its path's directory (parser/kbuild_symbols.py)."""

    def run() -> dict[str, Any]:
        import os
        from parser.kbuild_parser import KbuildParser
        from parser.kbuild_symbols import binding_rows
        pairs, path_of, fnid_of = _kbuild_pairs(db)
        # Pairs without rows yet (the state of an interrupted backfill)
        todo: list[tuple[int, int]] = []
        for i in range(0, len(pairs), 1000):
            chunk = pairs[i:i + 1000]
            blobs = sorted({b for _f, b in chunk})
            rows_done = _query_rows(
                db,
                f"SELECT DISTINCT source_fnid, source_blob_id FROM m_kbuild_symbol WHERE source_blob_id IN ({','.join(['%s'] * len(blobs))})",
                tuple(blobs),
            )
            done = {(int(f), int(b)) for f, b in rows_done}
            todo.extend(p for p in chunk if p not in done)
        blob_ids = sorted({b for _f, b in todo})
        sha_of: dict[int, str] = {}
        for i in range(0, len(blob_ids), 1000):
            chunk = blob_ids[i:i + 1000]
            rows_hash = _query_rows(
                db,
                f"SELECT blob_id, blob_hash FROM m_file_blob WHERE blob_id IN ({','.join(['%s'] * len(chunk))})",
                tuple(chunk),
            )
            for bid, bhash in rows_hash:
                sha_of[int(bid)] = bytes(bhash).hex()
        progress = Progress("Backfill Kbuild", len(todo))
        parser = KbuildParser()
        rows: list[tuple] = []
        sent = 0
        columns = ("source_blob_id", "source_fnid", "idx", "symbol", "via", "line_no", "compile_mode", "kind",
                   "target_obj", "target_path", "target_fnid", "rule")
        for fnid, blob_id in todo:
            path, sha = path_of.get(fnid), sha_of.get(blob_id)
            data = mf._read_blob(sha) if path and sha else None
            if data is not None:
                text = data.decode("latin-1")
                bindings = parser.parse_makefile_content(text, dir_path=os.path.dirname(path)) if text else []
                # binding_rows: (idx, line_no, symbol, via, ...) -> the table's (idx, symbol, via, line_no, ...)
                rows.extend((blob_id, fnid, r[0], r[2], r[3], r[1], *r[4:]) for r in binding_rows(bindings, fnid_of.get))
            if len(rows) >= INSERT_CHUNK:
                sent += insert_ignore(db, "m_kbuild_symbol", columns, rows)
                if hasattr(db, "commit") and callable(db.commit):
                    db.commit()
                rows = []
            progress.step()
        sent += insert_ignore(db, "m_kbuild_symbol", columns, rows)
        if hasattr(db, "commit") and callable(db.commit):
            db.commit()
        logger.info(f"[Backfill Kbuild] {len(todo)} Makefile (path, blob) pair(s), {sent} row(s) in {progress.seconds}s.")
        return {"state": "done", "pairs": len(todo), "rows": sent, "seconds": progress.seconds}

    return guarded("Backfill Kbuild", db, run)


# -----------------------------------------------------------------------------
# D04: m_commit_relation / m_commit_link (--backfill-commit-relations [--from-commit-id N])
# -----------------------------------------------------------------------------

RELATION_CHUNK = 5000


def commit_relations_state(db: Any) -> str:
    """The backfill state note of m_commit_relation (row commit_id 0, kind 0, idx 0): "" (never ran),
    "next:<id>" (interrupted after <id>) or "done:<id>" (every commit up to <id> covered; later ones are
    written by Step 6.5)."""
    sql = "SELECT note FROM m_commit_relation WHERE commit_id = 0 AND kind = 0 AND idx = 0"
    row = _query_one_row(db, sql)
    note = (row or ("",))[0] or ""
    return note.decode("utf-8", "replace") if isinstance(note, (bytes, bytearray)) else str(note)


def _set_relations_state(db: Any, note: str) -> None:
    from parser.git_ast.commit_relations import KIND_BACKFILL_STATE, ZERO_HASH
    db.execute(
        "INSERT INTO m_commit_relation (commit_id, kind, idx, target_hash, target_prefix, note) VALUES (0, %s, 0, %s, '', %s) "
        "ON DUPLICATE KEY UPDATE note = VALUES(note)",
        (KIND_BACKFILL_STATE, ZERO_HASH, note),
    )


def backfill_commit_relations(db: Any, repo_dir: str | None, from_commit_id: int | None = None) -> dict[str, Any]:
    """Parse the stored message of every commit (m_commit, all networks) into m_commit_relation and
    m_commit_link, RELATION_CHUNK commits per statement, abbreviated targets resolved against `repo_dir`
    once per chunk. Resumes after the state row's "next:<id>" (or `from_commit_id`); ends with "done:<id>"."""
    from parser.git_ast import commit_relations as cr
    from parser.git_ast.commit_relations import BACKFILL_DONE, BACKFILL_RUNNING

    def run() -> dict[str, Any]:
        state = commit_relations_state(db)
        if from_commit_id is not None:
            start = int(from_commit_id)
        elif state.startswith(BACKFILL_RUNNING):
            start = int(state[len(BACKFILL_RUNNING):] or 0)
        else:
            start = 0
        upper_row = db.query_one("SELECT COALESCE(MAX(commit_id), 0) FROM m_commit", dictionary=False)
        upper = int((upper_row or (0,))[0] or 0)
        progress = Progress("Backfill commit relations", max(0, upper - start))
        n_commits = n_rel = n_link = n_resolved = 0
        last = start
        while last < upper:
            batch = db.query(
                "SELECT commit_id, commit_hash, message FROM m_commit WHERE commit_id > %s AND commit_id <= %s "
                "ORDER BY commit_id LIMIT %s",
                (last, upper, RELATION_CHUNK),
                dictionary=False,
            )
            if not batch:
                break
            parsed = []
            for cid, chash, msg in batch:
                if isinstance(msg, (bytes, bytearray)):
                    msg = msg.decode("utf-8", "replace")
                parsed.append((int(cid), *cr.relation_rows(msg or "", bytes(chash).hex())))
            prefixes = set().union(*(cr.prefixes_to_resolve(r) for _c, r, _l in parsed))
            resolved = cr.resolve_prefixes(repo_dir, prefixes) if prefixes and repo_dir else {}
            rel_rows: list[tuple] = []
            link_rows: list[tuple] = []
            for cid, rels, links in parsed:
                r, lk = cr.table_rows(cid, rels, links, resolved)
                rel_rows.extend(r)
                link_rows.extend(lk)
            n_rel += insert_ignore(db, "m_commit_relation",
                                   ("commit_id", "kind", "idx", "target_hash", "target_prefix", "note"), rel_rows)
            n_link += insert_ignore(db, "m_commit_link", ("commit_id", "message_id"), link_rows)
            n_resolved += len(resolved)
            last = int(batch[-1][0])
            _set_relations_state(db, f"{BACKFILL_RUNNING}{last}")
            if hasattr(db, "commit") and callable(db.commit):
                db.commit()
            n_commits += len(batch)
            progress.step(int(batch[-1][0]) - (progress.done + start), f"commits ({n_rel} relations, {n_link} links)")
        _set_relations_state(db, f"{BACKFILL_DONE}{upper}")
        if hasattr(db, "commit") and callable(db.commit):
            db.commit()
        logger.info(f"[Backfill commit relations] {n_commits} commit(s), {n_rel} relation(s), {n_link} link(s), "
                    f"{n_resolved} abbreviated target(s) resolved, in {progress.seconds}s.")
        return {"state": "done", "commits": n_commits, "relations": n_rel, "links": n_link, "through": upper,
                "seconds": progress.seconds}

    return guarded("Backfill commit relations", db, run)


# -----------------------------------------------------------------------------
# D07: m_blob_facts (--backfill-facts [--families 1,2,3,4,5])
# -----------------------------------------------------------------------------

def fact_blob_paths(db: Any, versions: list[tuple[str, int, int]]) -> dict[bytes, str]:
    """blob SHA -> one path holding it, for every (path, blob) of the finished versions' checkpoint rows that has a
    fact family (read per version by primary key; delta checkpoints only hold changed rows, their union still
    covers every blob)."""
    from parser.facts_ingest import families_of
    fnid_of_blob: dict[int, int] = {}
    for _name, cid, _base in versions:
        sql_bc = "SELECT fnid, blob_id FROM m_bridge_checkpoint WHERE commit_id = %s AND blob_id > 0"
        bc_rows = _query_rows(db, sql_bc, (cid,))
        for fnid, bid in bc_rows:
            fnid_of_blob.setdefault(int(bid), int(fnid))
    path_of: dict[int, str] = {}
    fnids = sorted(set(fnid_of_blob.values()))
    for i in range(0, len(fnids), 1000):
        chunk = fnids[i:i + 1000]
        sql_fn = f"SELECT fnid, fname FROM m_file_name WHERE fnid IN ({','.join(['%s'] * len(chunk))})"
        fn_rows = _query_rows(db, sql_fn, tuple(chunk))
        for fnid, fname in fn_rows:
            path_of[int(fnid)] = fname.decode("utf-8", "replace") if isinstance(fname, (bytes, bytearray)) else str(fname)
    wanted = sorted(b for b, f in fnid_of_blob.items() if families_of(path_of.get(f, "")))
    out: dict[bytes, str] = {}
    for i in range(0, len(wanted), 1000):
        chunk = wanted[i:i + 1000]
        sql_fb = f"SELECT blob_id, blob_hash FROM m_file_blob WHERE blob_id IN ({','.join(['%s'] * len(chunk))})"
        fb_rows = _query_rows(db, sql_fb, tuple(chunk))
        for bid, bhash in fb_rows:
            out.setdefault(bytes(bhash), path_of[fnid_of_blob[int(bid)]])
    return out


def backfill_facts(db: Any, mf: Any, network_id: int, repo_id: int, families: Iterable[int] | None = None,
                   workers: int = 1) -> dict[str, Any]:
    """Fill m_blob_facts for the blobs of every finished version (all families, or `families`), as Step 6.9 would
    have: blobs read from Git by SHA, families a blob already has in their current format skipped (the work list
    and the resume state: facts_ingest.missing())."""
    from parser import facts_ingest

    def run() -> dict[str, Any]:
        versions = finished_versions(db, network_id, repo_id)
        blobs = fact_blob_paths(db, versions)
        progress = Progress("Backfill facts", len(blobs))

        def read_sha(sha: bytes) -> str | None:
            data = mf._read_blob(sha.hex())
            return data.decode("utf-8", "replace") if data is not None else None

        result = facts_ingest.ingest(db, blobs, lambda _p: None, workers, families=families, read_sha=read_sha,
                                     progress=progress.step)
        logger.info(f"[Backfill facts] {len(versions)} version(s), {len(blobs)} blob(s) with a family, "
                    f"{result['extracted']} extracted ({result['rows']} rows) in {progress.seconds}s.")
        return {"state": "done", "versions": len(versions), **result}

    return guarded("Backfill facts", db, run)


# -----------------------------------------------------------------------------
# D08: m_file_reference (--backfill-doc-references)
# -----------------------------------------------------------------------------

def backfill_doc_references(db: Any, mf: Any, network_id: int, repo_id: int, workers: int = 1) -> dict[str, Any]:
    """Fill m_file_reference with ref_type = 5 (FileRefType.Documentation) for documentation and text files
    across finished version checkpoints (core/API.md section 12)."""
    from core.globalstuff import FileRefType
    from parser.doc_parser import DocReferenceScanner

    def run() -> dict[str, Any]:
        rows_fn = _query_rows(db, "SELECT fnid, fname FROM m_file_name WHERE fnid > 0")
        path_of: dict[int, str] = {}
        fnid_of: dict[str, int] = {}
        scanner = DocReferenceScanner()
        for fnid, fname in rows_fn:
            if isinstance(fname, (bytes, bytearray)):
                fname = fname.decode("utf-8", "replace")
            fnid_of[fname] = int(fnid)
            if scanner.is_doc_candidate(fname):
                path_of[int(fnid)] = fname

        fnids = sorted(path_of)
        versions = finished_versions(db, network_id, repo_id)
        commit_ids = [cid for _ref, cid, _base in versions]

        pairs: set[tuple[int, int]] = set()
        for cid in commit_ids:
            for i in range(0, len(fnids), 2000):
                chunk = fnids[i:i + 2000]
                rows_bp = _query_rows(
                    db,
                    f"SELECT fnid, blob_id FROM m_bridge_checkpoint WHERE commit_id = %s "
                    f"AND fnid IN ({','.join(['%s'] * len(chunk))}) AND blob_id > 0",
                    (cid, *chunk),
                )
                pairs.update((int(f), int(b)) for f, b in rows_bp)

        doc_type = int(FileRefType.Documentation)
        sorted_pairs = sorted(pairs)
        todo: list[tuple[int, int]] = []
        for i in range(0, len(sorted_pairs), 1000):
            chunk = sorted_pairs[i:i + 1000]
            blob_ids = sorted({b for _f, b in chunk})
            rows_ref = _query_rows(
                db,
                f"SELECT DISTINCT source_fnid, source_blob_id FROM m_file_reference "
                f"WHERE ref_type = %s AND source_blob_id IN ({','.join(['%s'] * len(blob_ids))})",
                (doc_type, *blob_ids),
            )
            done = {(int(f), int(b)) for f, b in rows_ref}
            todo.extend(p for p in chunk if p not in done)

        blob_ids = sorted({b for _f, b in todo})
        sha_of: dict[int, str] = {}
        for i in range(0, len(blob_ids), 1000):
            chunk = blob_ids[i:i + 1000]
            rows_blob = _query_rows(
                db,
                f"SELECT blob_id, blob_hash FROM m_file_blob WHERE blob_id IN ({','.join(['%s'] * len(chunk))})",
                tuple(chunk),
            )
            for bid, bhash in rows_blob:
                sha_of[int(bid)] = bytes(bhash).hex()

        progress = Progress("Backfill doc references", len(todo))
        rows: list[tuple] = []
        sent = 0
        columns = ("source_blob_id", "source_fnid", "target_fnid", "ref_type", "line_no", "details")

        for fnid, blob_id in todo:
            path, sha = path_of.get(fnid), sha_of.get(blob_id)
            data = mf._read_blob(sha) if path and sha and hasattr(mf, "_read_blob") else None
            if data is not None:
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    text = data.decode("latin-1", errors="replace")
                refs = scanner.scan_file_content(text, blob_id, fnid_of, source_path=path)
                for r in refs:
                    rows.append((blob_id, fnid, r.target_fnid, doc_type, int(r.line_no), r.details[:255]))

            if len(rows) >= INSERT_CHUNK:
                sent += insert_ignore(db, "m_file_reference", columns, rows)
                if hasattr(db, "commit") and callable(db.commit):
                    db.commit()
                rows = []
            progress.step()

        sent += insert_ignore(db, "m_file_reference", columns, rows)
        if hasattr(db, "commit") and callable(db.commit):
            db.commit()

        logger.info(f"[Backfill doc references] {len(todo)} doc (path, blob) pair(s), {sent} row(s) in {progress.seconds}s.")
        return {"state": "done", "pairs": len(todo), "rows": sent, "seconds": progress.seconds}

    return guarded("Backfill doc references", db, run)


# -----------------------------------------------------------------------------
# D09: m_cve_file (--backfill-cve-files)
# -----------------------------------------------------------------------------

def backfill_cve_files(db: Any, mf: Any = None, network_id: int = 1, repo_id: int = 1, workers: int = 1) -> dict[str, Any]:
    """Fill m_cve_file (cve_id, file_path) from parsed m_cve.data (core/API.md section 12)."""
    import json
    import zlib

    def run() -> dict[str, Any]:
        count_row = _query_one_row(db, "SELECT COUNT(*) FROM m_cve")
        total = int((count_row or (0,))[0] or 0)
        progress = Progress("Backfill CVE files", total)

        cve_batch = _query_rows(db, "SELECT cve_id, data FROM m_cve")

        rows: list[tuple[str, str]] = []
        sent = 0
        columns = ("cve_id", "file_path")

        for cve_id, data in cve_batch:
            if isinstance(cve_id, (bytes, bytearray)):
                cve_id = cve_id.decode("utf-8", "replace")
            if data:
                try:
                    raw = zlib.decompress(bytes(data))
                    rec = json.loads(raw)
                except Exception:
                    rec = {}
                files: list[str] = list(rec.get("files") or [])
                for rng in rec.get("ranges") or []:
                    if isinstance(rng, dict):
                        files.extend(rng.get("files") or [])
                seen_files: set[str] = set()
                for f in files:
                    if f and isinstance(f, str):
                        f_clean = f.strip()[:255]
                        if f_clean and f_clean not in seen_files:
                            seen_files.add(f_clean)
                            rows.append((str(cve_id), f_clean))

            if len(rows) >= INSERT_CHUNK:
                sent += insert_ignore(db, "m_cve_file", columns, rows)
                if hasattr(db, "commit") and callable(db.commit):
                    db.commit()
                rows = []
            progress.step()

        sent += insert_ignore(db, "m_cve_file", columns, rows)
        if hasattr(db, "commit") and callable(db.commit):
            db.commit()

        logger.info(f"[Backfill CVE files] {total} CVE record(s), {sent} row(s) in {progress.seconds}s.")
        return {"state": "done", "cves": total, "rows": sent, "seconds": progress.seconds}

    return guarded("Backfill CVE files", db, run)


# -----------------------------------------------------------------------------
# D10: m_commit_email (--backfill-mailing-list [--ml-range <range>])
# -----------------------------------------------------------------------------

def backfill_mailing_list(
    db: Any,
    repo_dir: str | None,
    commit_range: str | None = None,
    storage_dir: str | None = None,
) -> dict[str, Any]:
    """Execute public-inbox mailing list resolution across commit_range and stage into m_commit_email."""
    from parser.git_ast import GitCommitParser
    from parser.mailing_list.ml_client import MailingListClient
    from parser.mailing_list.ml_types import CommitFeatures, MLRelationType

    def run() -> dict[str, Any]:
        rng = commit_range or "HEAD~100..HEAD"
        r_dir = repo_dir or "."
        git_parser = GitCommitParser(repo_dir=r_dir)
        if ".." in rng:
            old_rev, new_rev = rng.split("..", 1)
        else:
            old_rev, new_rev = None, rng

        commits = git_parser.parse_version_commits(old_rev, new_rev, with_files=False)
        if not commits:
            logger.warning(f"[Backfill mailing list] No commits found in range '{rng}'.")
            return {"state": "done", "commits": 0, "rows": 0, "seconds": 0.0}

        commit_hashes = [c.commit_hash for c in commits]
        commit_hash_to_id: dict[str, int] = {}
        for i in range(0, len(commit_hashes), 1000):
            chunk = commit_hashes[i:i + 1000]
            rows_c = _query_rows(
                db,
                f"SELECT commit_hash, commit_id FROM m_commit WHERE commit_hash IN ({','.join(['%s'] * len(chunk))})",
                tuple(bytes.fromhex(h) for h in chunk),
            )
            for chash, cid in rows_c:
                commit_hash_to_id[bytes(chash).hex()] = int(cid)

        target_commits = [c for c in commits if c.commit_hash in commit_hash_to_id]
        if not target_commits:
            logger.warning("[Backfill mailing list] No commits from range exist in m_commit.")
            return {"state": "done", "commits": 0, "rows": 0, "seconds": 0.0}

        progress = Progress("Backfill mailing list", len(target_commits))
        client = MailingListClient(storage_dir=storage_dir) if storage_dir else MailingListClient()

        columns = ("commit_id", "message_id", "thread_id", "relation_type", "match_tier", "patch_version", "patch_idx", "patch_total")
        sent = 0
        BATCH_SIZE = 500

        for i in range(0, len(target_commits), BATCH_SIZE):
            chunk = target_commits[i:i + BATCH_SIZE]
            features_list: list[CommitFeatures] = []
            for c in chunk:
                cid = commit_hash_to_id[c.commit_hash]
                parents = getattr(c, "parents", None) or []
                features = CommitFeatures(
                    commit_id=cid,
                    commit_hash=c.commit_hash,
                    subject=getattr(c, "subject", "") or "",
                    author_name=getattr(c, "author_name", "") or "",
                    author_email=getattr(c, "author_email", "") or "",
                    author_date=int(getattr(c, "author_date", 0) or 0),
                    commit_ts=int(getattr(c, "committer_date", 0) or getattr(c, "author_date", 0) or 0),
                    commit_message=getattr(c, "message", "") or "",
                    is_merge=len(parents) >= 2,
                )
                features_list.append(features)

            results = client.resolve_commits(features_list)
            rows: list[tuple] = []
            for c_hash, ctx in results.items():
                cid = commit_hash_to_id.get(c_hash)
                if cid is None:
                    continue
                if ctx.patch_email and ctx.patch_email.mids:
                    mid = ctx.patch_email.mids[0].strip("<> ")
                    if mid:
                        rows.append((cid, mid[:255], ctx.patch_email.tid, int(MLRelationType.Patch),
                                     int(ctx.matched_tier), int(ctx.patch_version), int(ctx.patch_idx), int(ctx.patch_total)))
                if ctx.cover_letter and ctx.cover_letter.mids:
                    mid = ctx.cover_letter.mids[0].strip("<> ")
                    if mid:
                        rows.append((cid, mid[:255], ctx.cover_letter.tid, int(MLRelationType.CoverLetter),
                                     int(ctx.matched_tier), int(ctx.patch_version), 0, int(ctx.patch_total)))
                if ctx.pull_request and ctx.pull_request.mids:
                    mid = ctx.pull_request.mids[0].strip("<> ")
                    if mid:
                        rows.append((cid, mid[:255], ctx.pull_request.tid, int(MLRelationType.PullRequest),
                                     int(ctx.matched_tier), 1, 0, 0))

            sent += insert_ignore(db, "m_commit_email", columns, rows)
            if hasattr(db, "commit") and callable(db.commit):
                db.commit()
            progress.step(len(chunk))

        logger.info(f"[Backfill mailing list] {len(target_commits)} commit(s), {sent} row(s) in {progress.seconds}s.")
        return {"state": "done", "commits": len(target_commits), "rows": sent, "seconds": progress.seconds}

    return guarded("Backfill mailing list", db, run)
