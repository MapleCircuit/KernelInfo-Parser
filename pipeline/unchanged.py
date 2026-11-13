"""Carry-over of unchanged files, directories and symlinks between versions."""
from core.globalstuff import (
    G,
    T_DIR,
)
import os

from core.TableHandling import ChangeSet
from core.DBLayout import (
    m_file_name,
    m_file,
    m_bridge_file,
)
from pipeline.blob_reuse import _known_blob_id, _tagged_blob_ids
from pipeline.changesets import extract_tags_and_evacuate_cs, file_fid_cache, file_fnid_cache, get_fid_for_path
from pipeline.file_processing import file_processing
from pipeline.state import MF, gp, logger




def processing_unchanges() -> list[str]:
    """Carry files outside gp.Change_List forward into gp.VID and return the files that still need parsing.

    Files the diff missed (forgotten deletes and additions) and unchanged files absent from the previous
    version (self-healing) are returned as change lines for the parsing workers instead of being parsed
    serially here.
    """
    to_parse: list[str] = []
    if gp.Old_VID == 0:
        return to_parse
    full_set = set(MF.git_file_list(gp.Version_Name).splitlines())

    changed_set = set()
    deleted_set = set()

    for item in gp.Change_List:
        if not item:
            continue
        parts = item.split("\t")
        op = parts[0]
        if op.startswith("D"):
            deleted_set.add(parts[-1])
        elif op.startswith("R"):
            if len(parts) >= 3:
                deleted_set.add(parts[1])
                changed_set.add(parts[2])
            else:
                changed_set.add(parts[-1])
        else:
            changed_set.add(parts[-1])

    unchanged_set = full_set - changed_set

    old_full_set = set(MF.git_file_list(gp.Old_Version_Name).splitlines())
    forgotten_delete = (old_full_set - full_set) - deleted_set

    if forgotten_delete:
        logger.warning("There seems to be forgotten deletes... Processing...")
        if G.OVERRIDE_FORGOTTEN_PRINT:
            logger.debug(forgotten_delete)
        to_parse.extend(f"D\t{x}" for x in sorted(forgotten_delete))

    forgotten_new = (full_set - old_full_set) - changed_set
    if forgotten_new:
        logger.warning(f"Found {len(forgotten_new)} forgotten new files... Processing...")
        if G.OVERRIDE_FORGOTTEN_PRINT:
            logger.debug(forgotten_new)
        to_parse.extend(f"A\t{x}" for x in sorted(forgotten_new))

    te_set = G.TE.set
    t_id = m_bridge_file.table_id
    vid = gp.VID
    old_vid = gp.Old_VID

    working_dir = MF.version_dict.get(gp.Version_Name)
    missing_unchanged = []
    for unchanged in unchanged_set:
        un_m_file_name = m_file_name.get(None, unchanged)
        if un_m_file_name is None:
            missing_unchanged.append(unchanged)
            continue
        fnid = un_m_file_name[2][0]
        file_fnid_cache[unchanged] = fnid

        # Guard against duplicate primary key insertion if already checkpointed
        if m_bridge_file.get(vid, fnid, None) is not None:
            continue

        un_m_bridge_file = m_bridge_file.get(old_vid, fnid, None)

        if un_m_bridge_file is None:
            missing_unchanged.append(unchanged)
            continue

        # If unchanged is a symlink, defer resolution to processing_symlinks so target fid stays in sync
        if working_dir and os.path.islink(os.path.join(working_dir, unchanged)):
            gp.Symlink_List.append(f"U\t{unchanged}")
            continue

        te_set(
            t_id,
            (
                vid,
                fnid,
                un_m_bridge_file[2][2],
            ),
        )
        file_fid_cache[unchanged] = un_m_bridge_file[2][2]

    if missing_unchanged:
        missing_syms = []
        missing_regs = []
        for x in missing_unchanged:
            if working_dir and os.path.islink(os.path.join(working_dir, x)):
                missing_syms.append(f"A\t{x}")
            else:
                missing_regs.append(f"A\t{x}")

        if missing_regs:
            logger.warning(
                f"Found {len(missing_regs)} files in unchanged_set missing from prior DB version (Old_VID {old_vid}). "
                f"Self-healing by processing as new additions in VID {vid}..."
            )
            to_parse.extend(sorted(missing_regs))

        if missing_syms:
            gp.Symlink_List.extend(missing_syms)

    return to_parse


def processing_dirs() -> dict[str, ChangeSet]:  # noqa: C901
    """Carry unchanged directories forward into gp.VID and return ChangeSets for new directories."""
    dir_changesets: dict[str, ChangeSet] = {}
    dir_list = set(MF.get_dir_list(gp.Version_Name))

    if gp.Old_VID != 0:

        old_dir_list = set(MF.get_dir_list(gp.Old_Version_Name))
        new_dir_list = dir_list - old_dir_list
        unchanged_dirs = dir_list & old_dir_list

        # Unchanged dirs
        for single_dir in unchanged_dirs:
            # Get m_file_name
            un_m_file_name = m_file_name.get(None, single_dir)
            if un_m_file_name is None or not un_m_file_name[2]:
                new_dir_list.add(single_dir)
                logger.warning(f"Unchanged dir missing m_file_name: '{single_dir}', self-healing as new addition...")
                continue
            dir_fnid = un_m_file_name[2][0]
            if m_bridge_file.get(gp.VID, dir_fnid, None) is not None:
                continue
            # Get old_m_bridge_file
            old_m_bridge_file = m_bridge_file.get(gp.Old_VID, dir_fnid, None)
            if old_m_bridge_file is None or not old_m_bridge_file[2]:
                new_dir_list.add(single_dir)
                logger.warning(
                    f"Unchanged dir '{single_dir}' missing prior m_bridge_file (Old_VID {gp.Old_VID}). "
                    f"Self-healing as new addition in VID {gp.VID}..."
                )
                continue
            G.TE.set(
                m_bridge_file.table_id,
                (
                    gp.VID,
                    dir_fnid,
                    old_m_bridge_file[2][2],
                ),
            )

        # New dirs
        for single_dir in new_dir_list:
            CS = ChangeSet("A", single_dir)
            # 0 Check if FNAME exist/Create FNAME
            CS.store(m_file_name.get_set(None, single_dir))
            # 1 Create FILE
            CS.store(m_file.set(None, b"\x00" * 20, T_DIR))
            # 2 Create BRIDGE FILE
            CS.store(m_bridge_file.set(
                gp.VID,
                CS.ref(m_file_name.fnid),
                CS.ref(m_file.fid),
            ))
            dir_changesets[single_dir] = CS

    else:
        # If VID = 1, we need all dirs to be added
        for single_dir in dir_list:
            CS = ChangeSet("A", single_dir)
            # 0 Check if FNAME exist/Create FNAME
            CS.store(m_file_name.get_set(None, single_dir))
            # 1 Create FILE
            CS.store(m_file.set(None, b"\x00" * 20, T_DIR))
            # 2 Create BRIDGE FILE
            CS.store(m_bridge_file.set(
                gp.VID,
                CS.ref(m_file_name.fnid),
                CS.ref(m_file.fid),
            ))
            dir_changesets[single_dir] = CS
    return dir_changesets


def processing_symlinks(override_symlinks: list[str] | None = None) -> None:
    """Resolve symbolic links by aliasing to target fid in m_bridge_file without duplicating AST tags."""
    symlink_list = override_symlinks if override_symlinks is not None else getattr(gp, "Symlink_List", None)
    if not symlink_list or gp.VID == 0:
        return

    working_dir = MF.version_dict.get(gp.Version_Name)
    if not working_dir:
        return

    te_set = G.TE.set
    t_id = m_bridge_file.table_id
    vid = gp.VID

    # Deduplicate paths while preserving order
    seen_paths = set()
    unique_symlinks = []
    for item in symlink_list:
        parts = item.split("\t")
        sym_path = parts[-1]
        if sym_path not in seen_paths:
            seen_paths.add(sym_path)
            unique_symlinks.append((parts[0], sym_path))

    fallback_files = []

    for op, symlink_path in unique_symlinks:
        full_p = os.path.join(working_dir, symlink_path)
        try:
            raw_target = os.readlink(full_p)
        except OSError:
            fallback_files.append(f"{op}\t{symlink_path}")
            continue

        # Normalize target relative to repository root
        norm_target = os.path.normpath(os.path.join(os.path.dirname(symlink_path), raw_target))
        if norm_target.startswith("./"):
            norm_target = norm_target[2:]

        # Lookup target fid in current version
        target_fid = get_fid_for_path(norm_target)

        if target_fid is not None:
            sym_fn_res = G.TE.set(m_file_name.table_id, (None, symlink_path))
            sym_fnid = sym_fn_res[0]
            te_set(t_id, (vid, sym_fnid, target_fid))
            file_fid_cache[symlink_path] = target_fid
            file_fnid_cache[symlink_path] = sym_fnid
            if hasattr(gp, "file_blob_ids") and isinstance(gp.file_blob_ids, dict):
                gp.file_blob_ids[symlink_path] = target_fid
            logger.info(f"Symlink aliased: '{symlink_path}' -> '{norm_target}' (fid {target_fid})")
        else:
            logger.info(
                f"Symlink target '{norm_target}' not found in active tree; falling back to raw processing for '{symlink_path}'"
            )
            fallback_op = "A" if op == "U" else op
            fallback_files.append(f"{fallback_op}\t{symlink_path}")

    if fallback_files:
        fallback_files = _reuse_parsed_symlink_blobs(fallback_files)

    if fallback_files:
        file_processing(0, 0, fallback_files)
        for fb in fallback_files:
            fb_path = fb.split("\t")[-1]
            if fb_path in gp.ChangeSet_Dict:
                cs_obj = gp.ChangeSet_Dict[fb_path]
                cs_obj.gp = gp
                # Runs after every file of the version executed: unresolved references fall back to stubs
                if not cs_obj.execute():
                    cs_obj.execute(force_stubs=True)
                extract_tags_and_evacuate_cs(cs_obj)

    if override_symlinks is None:
        gp.Symlink_List.clear()


def _reuse_parsed_symlink_blobs(fallback_files: list[str]) -> list[str]:
    """Link fallback symlinks whose blob was already parsed (e.g. an unchanged broken symlink) to that blob.

    Parsing them again would stage a second m_file_blob row for the same `(blob_hash, ftype)` and m_bridge_tag
    rows the blob already has.

    Returns:
        The fallback change lines that still need parsing.
    """
    blob_hashes = getattr(gp, "file_blob_hashes", None) or {}
    parsed: dict[str, int] = {}
    for item in fallback_files:
        fpath = item.split("\t")[-1]
        b_hash = blob_hashes.get(fpath)
        if item.startswith("D") or not b_hash:
            continue
        blob_id = _known_blob_id(fpath, b_hash)
        if blob_id is not None:
            parsed[fpath] = blob_id

    tagged = _tagged_blob_ids(parsed.values())
    remaining = []
    for item in fallback_files:
        fpath = item.split("\t")[-1]
        b_id = parsed.get(fpath)
        if b_id not in tagged:
            remaining.append(item)
            continue
        fnid = G.TE.set(m_file_name.table_id, (None, fpath))[0]
        if m_bridge_file.get(gp.VID, fnid, None) is None:
            G.TE.set(m_bridge_file.table_id, (gp.VID, fnid, b_id))
        file_fid_cache[fpath] = b_id
        file_fnid_cache[fpath] = fnid
        gp.file_blob_ids[fpath] = b_id
        logger.info(f"Symlink '{fpath}' reuses its already parsed blob (fid {b_id})")
    return remaining
