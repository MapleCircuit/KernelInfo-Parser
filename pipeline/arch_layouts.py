"""Per-architecture Kbuild layouts."""
from core.globalstuff import (
    G,
)

import time
from typing import Any
from parser.arch_layout import compute_layout_rows, layout_digest
from parser.make_eval import Tree
from pipeline.git_network import kernel_steps_apply
from pipeline.state import MF, gp, logger




# Input traces and rows of the previous version's ARCH layouts, for the run (Step 6.8)
_ARCH_LAYOUT_MEMO: dict[str, Any] = {}


def _arch_layout_ids(db: Any, digests: list[bytes]) -> dict[bytes, int]:
    """layout_id of every stored m_arch_layout row whose layout_hash is one of `digests`."""
    rows = db.select_rows("m_arch_layout", ["layout_hash", "layout_id"], {"layout_hash": digests}, as_dict=False)
    return {bytes(h): int(i) for h, i in rows}


def processing_arch_layouts(version: str) -> int:
    """Store what `make ARCH=<arch>` uses at this version for every ARCH value (parser/arch_layout.py):
    source and include directories, build-time symlinks, generated asm-generic wrappers and forced
    includes, evaluated from the version's own Makefiles. A layout is stored once (m_arch_layout, keyed by
    `layout_digest()`, its rows in m_arch_layout_entry); the version's m_arch_root rows point at it.
    Returns the number of layout rows written (0 when every layout already existed). Skipped for a project that
    is not a kernel tree (`kernel_steps_apply()`) and for a tree with neither a top Makefile nor an `arch/` file."""
    if gp.VID == 0 or not getattr(gp, "file_blob_hashes", None) or not kernel_steps_apply():
        return 0
    if "Makefile" not in gp.file_blob_hashes and not any(p.startswith("arch/") for p in gp.file_blob_hashes):
        return 0
    tree = Tree(gp.file_blob_hashes)

    def read(path: str) -> str | None:
        try:
            return MF.get_file(path, gp.Version_Name)
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug(f"[Arch Layout] cannot read {path}: {exc}")
            return None

    started = time.perf_counter()
    # ARCH values whose inputs (files read, tree queries) are unchanged since the previous version of this
    # run reuse its rows (parser/arch_layout.py compute_layout_rows: replayed input traces)
    blobs = gp.file_blob_hashes
    by_arch, _ARCH_LAYOUT_MEMO["memo"], reused = compute_layout_rows(tree, read, blobs.get, _ARCH_LAYOUT_MEMO.get("memo"))
    layouts = by_arch
    written = new_layouts = 0
    with G.DB() as db:
        # A database error propagates: the version stays INGEST_PARSING and is rolled back on the next start
        digests = {arch: layout_digest(rows) for arch, rows in by_arch.items()}
        with db.transaction():
            db.delete_rows("m_arch_root", {"commit_id": gp.VID})
            # One lookup of the known layouts, one insert of the missing ones, one read of their ids
            wanted = sorted(set(digests.values()))
            known = _arch_layout_ids(db, wanted)
            missing = [d for d in wanted if d not in known]
            if missing:
                db.insert_rows("m_arch_layout", ["layout_hash"], [(d,) for d in missing], on_conflict="ignore")
                known.update(_arch_layout_ids(db, missing))
                rows_by_digest = {digests[arch]: rows for arch, rows in by_arch.items()}
                for digest in missing:
                    layout_id = known.get(digest, 0)
                    rows = rows_by_digest[digest]
                    db.insert_rows(
                        "m_arch_layout_entry",
                        ["layout_id", "role", "priority", "path", "target"],
                        [(layout_id, *r) for r in rows],
                        on_conflict="update",
                        update_columns=["priority", "target"],
                        chunk_size=1000,
                    )
                    written += len(rows)
                    new_layouts += 1
            db.insert_rows(
                "m_arch_root",
                ["commit_id", "arch", "layout_id"],
                [(gp.VID, arch[:32], known.get(digest, 0)) for arch, digest in digests.items()],
                on_conflict="update",
                update_columns=["layout_id"],
            )
    logger.info(
        f"[Arch Layout] '{version}': {len(layouts)} ARCH values (reused {reused} of {len(layouts)} ARCH layouts), "
        f"{new_layouts} new layout(s) ({written} rows) in {time.perf_counter() - started:.1f}s."
    )
    return written
