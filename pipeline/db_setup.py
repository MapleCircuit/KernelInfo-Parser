"""Database startup: fingerprint seed, legacy/obsolete table drops, layout markers, indexes and views."""
from core.globalstuff import (
    COLOR,
)

import time
import secrets
from typing import Any
from core.DBLayout import (
    m_maintainer_section,
    m_maintainer_member,
    m_credits_entry,
    m_commit_email,
    m_file_reference,
    m_db_instance,
)
from pipeline.state import logger




def ensure_db_instance(db: Any) -> None:
    """Ensure that the database instance fingerprint table m_db_instance has at least 1 seed row."""
    try:
        empty_filter = (None,) * m_db_instance.length
        res = db.select(m_db_instance, empty_filter)
        if not res:
            logger.info("m_db_instance is empty; seeding initial database instance fingerprint...")
            init_row = ((secrets.token_hex(32), int(time.time())),)
            db.insert(m_db_instance, init_row)
    except Exception as e:
        logger.warning(f"ensure_db_instance check encountered error: {e}")


# Symbol views of the shared layout; a database that still has them as tables predates it
_SYMBOL_VIEW_NAMES = ("m_symbol_def", "m_symbol_ref")


def _legacy_symbol_tables(db: Any) -> list[str]:
    """Names of the symbol views (`db_engine/views.py`) that still exist as tables of the per-blob layout."""
    return sorted(db.existing_tables(_SYMBOL_VIEW_NAMES, include_views=False))


def drop_legacy_symbol_tables(db: Any) -> None:
    """On --reset, drop the per-blob symbol tables of the earlier layout so their names can become views."""
    legacy = _legacy_symbol_tables(db)
    if legacy:
        db.drop_tables(legacy, ignore_fk=True)
        logger.info(f"Dropped tables of the per-blob symbol layout: {', '.join(legacy)}")


# Kconfig tables of the earlier layout (table ids 16-19), dropped from the schema
OBSOLETE_KCONFIG_TABLES = ("m_kconfig_kbuild", "m_kconfig_tree", "m_kconfig_relation", "m_kconfig_symbol")


# Table ids of the earlier layout -> current ids (core/DBLayout.py). m_ast_attribute keeps id 35: view
# hashes (m_ast_hash) include the ids of the joined tables, so its id must never change.
_OLD_TABLE_IDS = {
    **{old: old - 5 for old in range(20, 35)},  # m_maintainer_person .. m_db_instance
    35: 34,                                       # m_ast_attribute
    **{old: old - 6 for old in range(36, 40)},  # m_git_network, m_repository, m_git_ref, m_commit_parent
    **{old: old - 5 for old in range(40, 49)},  # m_ingest_state .. m_commit_email
}


# m_kconfig_tree (old id 18) was a watermarked table; id 18 now is m_maintainer_member, which is not
_OBSOLETE_MARKER_ID = 18


def drop_obsolete_kconfig_tables(db: Any) -> None:
    """Drop the Kconfig tables the schema no longer has and renumber the watermarks of interrupted runs.

    The tables after them moved to other ids (`_OLD_TABLE_IDS`). `m_ingest_watermark` stores table ids,
    so the marks of a version left `INGEST_PARSING` before the upgrade are renumbered first, in one
    transaction per commit: a commit's marks are in the old numbering exactly when they include id 18
    (`m_kconfig_tree`, marked, whereas id 18 now names the unmarked `m_maintainer_member`), which keeps
    a re-run harmless.
    """
    present = db.existing_tables(OBSOLETE_KCONFIG_TABLES + ("m_ingest_watermark",), include_views=False)
    if "m_ingest_watermark" in present:
        old_style = [
            int(c) for c in db.select_values(
                "m_ingest_watermark", "commit_id", {"marked_table_id": _OBSOLETE_MARKER_ID}, distinct=True,
            )
        ]
        for commit_id in old_style:
            marks = [
                (int(t), int(m)) for t, m in db.select_rows(
                    "m_ingest_watermark", ["marked_table_id", "max_id"], {"commit_id": commit_id}, as_dict=False,
                )
            ]
            renumbered = []
            for table_id, max_id in marks:
                new_id = table_id if table_id < 16 else _OLD_TABLE_IDS.get(table_id)
                if new_id is not None:
                    renumbered.append((commit_id, new_id, max_id))
            with db.transaction():
                db.delete_rows("m_ingest_watermark", {"commit_id": commit_id})
                if renumbered:
                    db.insert_rows("m_ingest_watermark", ["commit_id", "marked_table_id", "max_id"], renumbered)
        if old_style:
            logger.info(f"Renumbered the ingestion watermarks of {len(old_style)} interrupted version(s) to the current table ids.")
    obsolete = [n for n in OBSOLETE_KCONFIG_TABLES if n in present]
    if obsolete:
        db.drop_tables(obsolete, ignore_fk=True)
        logger.info(f"Dropped Kconfig tables the schema no longer uses: {', '.join(obsolete)}")


# Columns of the current layout an older database lacks (or still has): such a database cannot be extended in place
_LAYOUT_MARKERS = (
    ("m_arch_root", "layout_id", True),          # build layouts shared between versions (m_arch_layout)
    ("m_blob_symbol", "target_fnid", False),     # target files moved to m_blob_symbol_file
)


def outdated_layout(db: Any) -> list[str]:
    """Why the database's tables predate the current layout (core/DBLayout.py); empty when they do not."""
    reasons = []
    for table, column, wanted in _LAYOUT_MARKERS:
        present = db.column_exists(table, column)
        if db.table_exists(table) and present != wanted:
            reasons.append(f"{table}.{column} {'missing' if wanted else 'still present'}")
    info = db.column_info("m_ast_hash", "hash")
    if info is not None and info.octet_length is not None and int(info.octet_length) != 16:
        reasons.append(f"m_ast_hash.hash is BINARY({int(info.octet_length)}), not BINARY(16)")
    return reasons


def drop_familyless_blob_facts(db: Any) -> bool:
    """Drop an `m_blob_facts` created before its `family` column (core/DBLayout.py); returns whether it was dropped.

    Its rows are derived from the blobs alone (Step 6.9 writes them again for the versions ingested next, and the
    webapp extracts what the database lacks), so the table is recreated empty rather than migrated."""
    if not db.table_exists("m_blob_facts") or db.column_exists("m_blob_facts", "family"):
        return False
    db.drop_tables(["m_blob_facts"], if_exists=False)
    logger.info("Dropped m_blob_facts (created before its family column); it is recreated empty.")
    return True


# Tables that hold derived data only, and the column whose presence (True) or absence (False) marks the
# layout before their current one. An outdated one is dropped at start and recreated empty by the
# missing-table check; its backfill refills it (plans/database/README.md rule 2).
_DERIVED_LAYOUT_MARKERS: tuple[tuple[str, str, bool, str], ...] = (
    ("m_maintainer_file", "maintainers_blob_id", True, "keyed by MAINTAINERS blob; refill with --backfill-maintainer-files"),
)


def drop_outdated_derived_tables(db: Any) -> list[str]:
    """Drop the derived tables of `_DERIVED_LAYOUT_MARKERS` that still have their old layout; returns their names."""
    dropped = []
    for table, column, old_when_present, note in _DERIVED_LAYOUT_MARKERS:
        if not db.table_exists(table):
            continue
        if db.column_exists(table, column) != old_when_present:
            continue
        db.drop_tables([table], if_exists=False)
        dropped.append(table)
        logger.info(f"Dropped {table} (older layout: {note}); it is recreated empty.")
    return dropped


# Tables whose declared secondary indexes are added to existing databases at start (indexes declared
# after the table was created; create_table() only applies them to new tables)
ENSURED_INDEX_TABLES = (m_commit_email, m_maintainer_member, m_maintainer_section, m_credits_entry, m_file_reference)


def ensure_declared_indexes(db: Any, tables: Any = ENSURED_INDEX_TABLES) -> list[str]:
    """Create the declared indexes of `tables` that an existing database lacks; returns their names."""
    created = []
    for table in tables:
        for name, cols in (getattr(table, "indexes", None) or ()):
            try:
                if db.index_exists(name, table):
                    continue
                logger.info(f"Creating index {name} on {table.table_name} (declared after the table was created)...")
                db.create_index(name, table, tuple(getattr(table, c) for c in cols))
                created.append(name)
            except Exception as exc:  # noqa: BLE001 - an index is an optimization, never a blocker
                logger.warning(f"Could not create index {name} on {table.table_name}: {exc}")
    return created


def ensure_db_views(db: Any) -> None:
    """Create or replace the SQL compatibility views registered in `db_engine/views.py`.

    Raises:
        RuntimeError: The database still has `m_symbol_def` / `m_symbol_ref` tables from before symbol rows were
            stored once per tag; its rows cannot be combined with the new layout, so it must be rebuilt (--reset).
    """
    legacy = _legacy_symbol_tables(db)
    if legacy:
        raise RuntimeError(
            f"The database predates the shared symbol layout ({', '.join(legacy)} are tables, not views). "
            "Rebuild it with --reset."
        )
    for vname, error in db.ensure_views().items():
        if error is None:
            continue
        if vname in _SYMBOL_VIEW_NAMES or vname == "m_checkpoint_tree":
            logger.warning(COLOR.yellow(f"ensure_db_views could not create view {vname}: {error}"))
        else:
            logger.debug(f"ensure_db_views skipping view {vname}: {error}")
