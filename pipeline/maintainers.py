"""MAINTAINERS file ingestion."""
from core.globalstuff import (
    G,
)

import logging
from typing import Any
from collections.abc import Iterable
from core.DBLayout import (
    m_maintainer_section,
    m_maintainer_file,
)
from pipeline.changesets import get_fid_for_path, get_fnid_for_path
from pipeline.git_network import kernel_steps_apply
from pipeline.state import MF, gp, logger




def _maintainer_scope(
    blob_id: int | None, base_commit_id: int, sections: Iterable[Any]
) -> tuple[dict[str, int], set[int], set[int], bool]:
    """(section key -> sec_id of this version, sec_ids of the base version, base sec_ids with file rows,
    whether SQL answered).

    Read through parser/maintainer_ast/maintainer_store.py. When the database cannot answer, the sections are
    looked up by name in the TableEngine and the base is unknown."""
    from parser.maintainer_ast import maintainer_store
    with G.DB() as db:
        try:
            if not blob_id:
                raise LookupError("no MAINTAINERS blob to read the sections of")
            cur_ids = maintainer_store.blob_section_ids(db, blob_id)
            prev_ids: set[int] = set()
            if base_commit_id:
                prev_blob = maintainer_store.version_blob(db, base_commit_id, "MAINTAINERS")
                prev_ids = set(maintainer_store.blob_section_ids(db, prev_blob).values()) if prev_blob else set()
            mapped = maintainer_store.sections_with_rows(db, prev_ids) if prev_ids else set()
            return cur_ids, prev_ids, mapped, True
        except Exception as exc:  # noqa: BLE001 - the TableEngine lookup below answers instead
            logger.log(logging.DEBUG if isinstance(exc, LookupError) else logging.WARNING,
                       f"Maintainer sections not read from the database ({exc}); using the TableEngine.")
    found: dict[str, int] = {}
    for sec in sections:
        key = maintainer_store.section_key(sec.name)
        if key and key not in found:
            row = m_maintainer_section.get(None, None, sec.name, None, None, None, None, None)
            if row and len(row) >= 3 and row[2]:
                found[key] = row[2][0]
    return found, set(), set(), False


def processing_maintainer_files(version: str) -> None:
    """Step 6.7: store which files each MAINTAINERS section version covers (m_maintainer_file).

    A (sec_id, fnid) row holds for every MAINTAINERS blob containing that section version, so only the
    sections new or changed since the base version are matched against every path, and the unchanged ones
    against the paths new in this version. Nothing is parsed when neither exists. The matching is not cut
    short by the duration limit: a section matched against part of the tree only would stay incomplete.
    Skipped for a project that is not a kernel tree (`kernel_steps_apply()`)."""
    if gp.VID == 0 or not kernel_steps_apply():
        return
    from parser.maintainer_ast import maintainer_store

    maintainers_blob_id = get_fid_for_path("MAINTAINERS")
    if maintainers_blob_id is None:
        logger.info(f"Version '{version}' has no MAINTAINERS blob row; skipping maintainer file matching.")
        return
    # A failing listing propagates: the version then stays INGEST_PARSING instead of being marked done
    # without m_maintainer_file rows
    file_list_raw = MF.git_file_list(gp.Version_Name)
    all_files = file_list_raw.splitlines() if file_list_raw else []
    if not all_files:
        return

    base_commit_id = int(getattr(gp, "Old_VID", 0) or 0)
    base_name = getattr(gp, "Old_Version_Name", None)
    cur_ids, prev_ids, mapped, has_sql = _maintainer_scope(maintainers_blob_id, base_commit_id, ())
    if has_sql and not cur_ids:
        logger.warning(f"No stored MAINTAINERS sections found for the blob of '{version}'; skipping maintainer file matching.")
        return
    if has_sql:
        # A base whose sections have no row yet (first run after the migration, or a gap): match everything
        changed: set[int] | None = (set(cur_ids.values()) - prev_ids) if mapped else None
        new_paths: list[str] = []
        if base_name and base_commit_id:
            try:
                old_files = set((MF.git_file_list(base_name) or "").splitlines())
                new_paths = [p for p in all_files if p not in old_files]
            except Exception as e:
                logger.debug(f"Failed to list the base version '{base_name}' for maintainer matching: {e}")
                changed = None
        else:
            changed = None
        if changed is not None and not changed and not new_paths:
            logger.info(f"[Step 6.7] MAINTAINERS sections and paths of '{version}' are unchanged since '{base_name}': nothing to match.")
            return

    # A failing read propagates (see the file listing above)
    raw_maintainers = MF.get_file("MAINTAINERS", gp.Version_Name)
    if not raw_maintainers:
        return

    from parser.maintainer_ast.maintainer_parser import MaintainerParser
    sections = MaintainerParser(raw_maintainers).parse()
    if not sections:
        return
    if not has_sql:
        cur_ids = _maintainer_scope(None, 0, sections)[0]
        changed, new_paths = None, []

    n_changed = len(cur_ids) if changed is None else len(changed)
    logger.info(
        f"Matching {n_changed} changed MAINTAINERS section(s) against {len(all_files)} files"
        f" and {len(cur_ids)} against {len(new_paths)} new file(s) for version '{version}'..."
    )
    pairs = maintainer_store.map_files(sections, cur_ids, changed, all_files, new_paths)

    staged = 0
    for sec_id, path in sorted(pairs):
        fnid = get_fnid_for_path(path)
        if fnid is None:
            continue
        G.TE.set(m_maintainer_file.table_id, (sec_id, fnid))
        staged += 1
    logger.info(f"Staged {staged} file-to-section mappings into m_maintainer_file for version '{version}'.")
