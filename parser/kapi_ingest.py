"""parser/kapi_ingest.py - Materialize per-revision Kernel API catalogs & release summaries.

Reads per-blob Kernel API facts (`m_blob_facts`, family 2) — extracting any missing blobs via
`parser/facts_ingest.py` — assembles the 6 KAPI catalogs (`syscalls`, `exports`, `params`,
`tracepoints`, `modules`, `devices`) via `parser/kapi_catalog.py`, and writes `m_kapi_catalog` and
`m_kapi_release_summary` idempotently.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from collections.abc import Iterable, Sequence
from typing import Any

from parser import facts_ingest as fi
from parser import kapi_facts as kf
from parser.git_ast.release_order import release_key, release_tag_names
from parser.kapi_catalog import (
    ARCH_INDEPENDENT_KINDS,
    ARCH_SCOPED_KINDS,
    CATALOG_FORMAT,
    TextCache,
    build_device_index,
    build_exports,
    build_kbuild_index,
    build_modules,
    build_params,
    build_release_summary,
    build_syscalls,
    build_tracepoints,
    encode_catalog,
    resolve_arch_info,
    summarize_catalog,
)

logger = logging.getLogger(__name__)

KAPI_BLOB_SUFFIXES = (".c", ".h", ".S", ".s", ".rs")
_BATCH_CHUNK = 1000


class _GitCatBatch:
    """Persistent `git cat-file --batch` reader for one repository."""

    def __init__(self, repo_path: str) -> None:
        self.repo_path = repo_path
        self._proc: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> _GitCatBatch:
        self._proc = subprocess.Popen(
            ["git", "-C", self.repo_path, "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return self

    def __exit__(self, *_args: Any) -> None:
        if self._proc is not None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
            except OSError:
                pass
            try:
                if self._proc.stdout:
                    self._proc.stdout.close()
            except OSError:
                pass
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

    def read_object(self, spec: str) -> bytes | None:
        """Read a Git object by SHA or `<rev>:<path>`."""
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            out = subprocess.run(
                ["git", "-C", self.repo_path, "cat-file", "-p", spec],
                capture_output=True,
                check=False,
            )
            return out.stdout if out.returncode == 0 else None
        self._proc.stdin.write((spec + "\n").encode("utf-8"))
        self._proc.stdin.flush()
        header = self._proc.stdout.readline().decode("utf-8", "replace").strip()
        parts = header.split()
        if len(parts) < 3 or parts[1] == "missing":
            return None
        size = int(parts[2])
        data = self._proc.stdout.read(size)
        self._proc.stdout.read(1)  # trailing LF
        return data


def _list_tree_blobs(repo_path: str, rev: str) -> list[tuple[str, bytes, str]]:
    """Return `[(path, blob_sha_bytes, blob_sha_hex), ...]` for all blobs in `rev`."""
    proc = subprocess.run(
        ["git", "-C", repo_path, "ls-tree", "-r", "-z", rev],
        capture_output=True,
        check=True,
    )
    out: list[tuple[str, bytes, str]] = []
    for entry in proc.stdout.split(b"\0"):
        if not entry:
            continue
        meta, _, raw_path = entry.partition(b"\t")
        parts = meta.decode("ascii", "replace").split()
        if len(parts) < 3 or parts[1] != "blob":
            continue
        sha_hex = parts[2]
        path = raw_path.decode("utf-8", "replace")
        out.append((path, bytes.fromhex(sha_hex), sha_hex))
    return out


def _git_release_tags(repo_path: str) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", repo_path, "tag"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return []
    return release_tag_names(proc.stdout.splitlines())


def _resolve_commit_sha(repo_path: str, rev: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "--verify", f"{rev}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha if len(sha) == 40 else None


def _resolve_commit_id(db: Any, repo_path: str, repo_id: int | None, tag: str) -> int | None:
    if db is None:
        return None
    where: dict[str, Any] = {"ref_name": tag}
    if repo_id is not None:
        where["repo_id"] = int(repo_id)
    row = db.select_one("m_git_ref", ["commit_id"], where, order_by=["-commit_id"], as_dict=True)
    if row and row.get("commit_id") is not None:
        return int(row["commit_id"])
    sha = _resolve_commit_sha(repo_path, tag)
    if not sha:
        return None
    c_where: dict[str, Any] = {"commit_hash": bytes.fromhex(sha)}
    if repo_id is not None:
        rep = db.select_one("m_repository", ["network_id"], {"repo_id": int(repo_id)}, as_dict=True)
        if rep and rep.get("network_id") is not None:
            c_where["network_id"] = int(rep["network_id"])
    c_row = db.select_one("m_commit", ["commit_id"], c_where, order_by=["-commit_id"], as_dict=True)
    if c_row and c_row.get("commit_id") is not None:
        return int(c_row["commit_id"])
    return None


def _load_kapi_records(
    db: Any,
    batch: _GitCatBatch,
    tree_blobs: list[tuple[str, bytes, str]],
    *,
    workers: int = 1,
) -> list[tuple[str, dict[str, Any]]]:
    """Ensure `m_blob_facts` family 2 exists for `tree_blobs` and return decoded `(path, record)` pairs."""
    relevant = [(p, b_bytes, b_hex) for p, b_bytes, b_hex in tree_blobs if p.endswith(KAPI_BLOB_SUFFIXES)]
    blob_to_path: dict[bytes, str] = {}
    for p, b_bytes, _b_hex in relevant:
        blob_to_path.setdefault(b_bytes, p)

    def read_sha(sha: bytes) -> str | None:
        raw = batch.read_object(sha.hex())
        return None if raw is None else raw.decode("utf-8", "replace")

    if db is not None and blob_to_path:
        fi.ingest(
            db,
            blob_to_path,
            lambda p: None,
            workers=workers,
            families=(kf.FAMILY,),
            read_sha=read_sha,
        )

    decoded_by_sha: dict[bytes, dict[str, Any]] = {}
    shas = list(blob_to_path.keys())
    if db is not None:
        for i in range(0, len(shas), _BATCH_CHUNK):
            chunk = shas[i : i + _BATCH_CHUNK]
            rows = db.select_rows(
                "m_blob_facts",
                ["blob_hash", "data"],
                {"blob_hash": chunk, "family": kf.FAMILY, "format": kf.KAPI_FORMAT},
                as_dict=True,
            )
            for r in rows:
                b_hash = bytes(r["blob_hash"])
                raw_data = bytes(r["data"]) if r.get("data") is not None else b""
                rec = kf.decode(raw_data)
                if rec:
                    decoded_by_sha[b_hash] = rec
    else:
        for b_bytes, path in blob_to_path.items():
            text = read_sha(b_bytes)
            if text is None or len(text) > fi.MAX_TEXT:
                continue
            raw_data = fi._encoded(kf.FAMILY, text, None, path)
            rec = kf.decode(raw_data)
            if rec:
                decoded_by_sha[b_bytes] = rec

    return [(p, decoded_by_sha[b_bytes]) for p, b_bytes, _b_hex in relevant if b_bytes in decoded_by_sha]


def _already_materialized(db: Any, commit_id: int, arches: Sequence[str]) -> bool:
    if db is None:
        return False
    cat_rows = db.select_rows(
        "m_kapi_catalog",
        ["kind", "catalog_format", "kapi_format"],
        {"commit_id": int(commit_id), "arch": ""},
        as_dict=True,
    )
    have_shared = {
        r["kind"]
        for r in cat_rows
        if int(r.get("catalog_format") or 0) == CATALOG_FORMAT and int(r.get("kapi_format") or 0) == kf.KAPI_FORMAT
    }
    if not set(ARCH_INDEPENDENT_KINDS).issubset(have_shared):
        return False
    for arch in arches:
        sum_row = db.select_one(
            "m_kapi_release_summary",
            ["catalog_format", "kapi_format"],
            {"commit_id": int(commit_id), "arch": arch},
            as_dict=True,
        )
        if not sum_row:
            return False
        if (
            int(sum_row.get("catalog_format") or 0) != CATALOG_FORMAT
            or int(sum_row.get("kapi_format") or 0) != kf.KAPI_FORMAT
        ):
            return False
    return True


def _write_catalog_row(
    db: Any,
    commit_id: int,
    arch: str,
    kind: str,
    payload: dict[str, Any],
    built_at: int,
) -> dict[str, Any]:
    summary = summarize_catalog(kind, payload)
    blob = encode_catalog(payload)
    row = (
        int(commit_id),
        arch,
        kind,
        CATALOG_FORMAT,
        kf.KAPI_FORMAT,
        int(summary.get("total", 0)),
        json.dumps(summary, separators=(",", ":"), ensure_ascii=False),
        blob,
        int(built_at),
    )
    db.insert_rows(
        "m_kapi_catalog",
        [
            "commit_id",
            "arch",
            "kind",
            "catalog_format",
            "kapi_format",
            "total_count",
            "summary_json",
            "data",
            "built_at",
        ],
        [row],
        on_conflict="update",
        update_columns=["catalog_format", "kapi_format", "total_count", "summary_json", "data", "built_at"],
    )
    return summary


def ingest_kapi_release(
    db: Any,
    repo_path: str,
    repo_id: int | None,
    tag: str,
    *,
    commit_id: int | None = None,
    arches: Iterable[str] = ("x86_64",),
    force: bool = False,
    workers: int = 1,
) -> dict[str, Any]:
    """Materialize KAPI catalogs and release summary for a single revision `tag`."""
    started = time.perf_counter()
    arch_list = tuple(dict.fromkeys((a or "x86_64").strip().lower() for a in arches)) or ("x86_64",)
    cid = int(commit_id) if commit_id is not None else _resolve_commit_id(db, repo_path, repo_id, tag)
    if cid is None:
        return {"tag": tag, "status": "skipped", "reason": "no_commit_id", "seconds": 0.0}

    if not force and _already_materialized(db, cid, arch_list):
        return {"tag": tag, "commit_id": cid, "status": "skipped", "reason": "up_to_date", "seconds": 0.0}

    tree_blobs = _list_tree_blobs(repo_path, tag)
    path_to_sha = {p: b_hex for p, _b_bytes, b_hex in tree_blobs}
    paths = set(path_to_sha.keys())

    with _GitCatBatch(repo_path) as batch:
        records = _load_kapi_records(db, batch, tree_blobs, workers=workers)
        files = TextCache(lambda p: batch.read_object(path_to_sha[p]) if p in path_to_sha else None)

        built_at = int(time.time())
        shared_catalogs: dict[str, dict[str, Any]] = {
            "exports": build_exports(records),
            "tracepoints": build_tracepoints(records),
            "devices": build_device_index(files, records),
        }

        with db.transaction():
            for kind in ARCH_INDEPENDENT_KINDS:
                _write_catalog_row(db, cid, "", kind, shared_catalogs[kind], built_at)

            for arch in arch_list:
                info = resolve_arch_info(files, paths, arch)
                kbuild_idx = build_kbuild_index(files, paths, info["arch"], info["srcarch"])
                arch_catalogs: dict[str, dict[str, Any]] = {
                    "syscalls": build_syscalls(files, paths, records, info["arch"], info["srcarch"]),
                    "params": build_params(files, paths, records, info["srcarch"], kbuild_idx=kbuild_idx),
                    "modules": build_modules(records, kbuild_idx, arch=info["arch"]),
                }
                for kind in ARCH_SCOPED_KINDS:
                    _write_catalog_row(db, cid, arch, kind, arch_catalogs[kind], built_at)

                combined = {**shared_catalogs, **arch_catalogs}
                rel_sum = build_release_summary(combined)
                subsystems_json = json.dumps(
                    {"kinds": rel_sum["kinds"], "subsystems": rel_sum["subsystems"]},
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                db.insert_rows(
                    "m_kapi_release_summary",
                    [
                        "commit_id",
                        "arch",
                        "catalog_format",
                        "kapi_format",
                        "syscalls_count",
                        "exports_count",
                        "params_count",
                        "tracepoints_count",
                        "modules_count",
                        "devices_count",
                        "subsystems_json",
                        "built_at",
                    ],
                    [
                        (
                            cid,
                            arch,
                            CATALOG_FORMAT,
                            kf.KAPI_FORMAT,
                            rel_sum["syscalls_count"],
                            rel_sum["exports_count"],
                            rel_sum["params_count"],
                            rel_sum["tracepoints_count"],
                            rel_sum["modules_count"],
                            rel_sum["devices_count"],
                            subsystems_json,
                            built_at,
                        )
                    ],
                    on_conflict="update",
                    update_columns=[
                        "catalog_format",
                        "kapi_format",
                        "syscalls_count",
                        "exports_count",
                        "params_count",
                        "tracepoints_count",
                        "modules_count",
                        "devices_count",
                        "subsystems_json",
                        "built_at",
                    ],
                )

    elapsed = round(time.perf_counter() - started, 2)
    logger.info("KAPI materialized %s (commit_id=%d, arches=%s) in %.2f s", tag, cid, ",".join(arch_list), elapsed)
    return {"tag": tag, "commit_id": cid, "status": "built", "arches": list(arch_list), "seconds": elapsed}


def ingest_kapi(
    db: Any,
    repo_path: str,
    repo_id: int | None,
    *,
    from_tag: str | None = None,
    tags: Iterable[str] | None = None,
    arches: Iterable[str] = ("x86_64",),
    force: bool = False,
    workers: int = 1,
    repo_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Materialize KAPI catalogs & release summaries for selected tags of `repo_path`."""
    started = time.perf_counter()
    if tags is not None:
        candidate_tags = list(dict.fromkeys(tags))
    else:
        candidate_tags = _git_release_tags(repo_path)

    if repo_entry is not None:
        from core.config import resolve_preparse_tags
        selected = resolve_preparse_tags(repo_entry, "api", candidate_tags)
    else:
        selected = sorted(candidate_tags, key=release_key)

    if from_tag:
        fk = release_key(from_tag)
        selected = [t for t in selected if release_key(t) >= fk]

    built = 0
    skipped = 0
    errors: list[dict[str, str]] = []
    for tag in selected:
        try:
            res = ingest_kapi_release(
                db,
                repo_path,
                repo_id,
                tag,
                arches=arches,
                force=force,
                workers=workers,
            )
            if res["status"] == "built":
                built += 1
            else:
                skipped += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("KAPI ingest failed for %s: %s", tag, exc)
            errors.append({"tag": tag, "error": str(exc)})

    return {
        "tags": len(selected),
        "built": built,
        "skipped": skipped,
        "errors": errors,
        "seconds": round(time.perf_counter() - started, 2),
    }
