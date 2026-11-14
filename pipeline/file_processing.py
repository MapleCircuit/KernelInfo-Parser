"""Per-file parsing: `default_processing`, the worker body and `file_processing`."""
from core.globalstuff import (
    G,
    COLOR,
    REF_OLD,
    CONTINUE_EXCEPTION,
    reclaim_system_memory,
)
import sys

import time
from typing import Any
import copy
import queue
import threading
from collections import Counter
import pickle
import traceback
from parser.c_ast.tag_tracker import query_prior_tags_many
from core.FileHandler import MasterFile
from core.GreatProcessor import GreatProcessor
from core.TableHandling import ChangeSet
from core.SymbolRows import STATS as SYMBOL_STATS
from core.DBLayout import (
    m_file_name,
    m_file,
    m_bridge_file,
    m_moved_file,
    m_tag_def,
    m_tag_ref,
)
from pipeline.state import MF, gp, logger
from pipeline.worker_protocol import _FRAME_DONE, _FRAME_INIT_FAILED, _WORKER_SPLIT_KEYS, _encode_batch_result, _recv_task




def default_processing(CS: ChangeSet) -> None:
    """Create m_file_name, m_file, m_bridge_file."""
    try:
        if CS.file_operation == "D":
            # DELETE: the file is simply absent from gp.VID's checkpoint; parsers close its prior tags
            with CS(REF_OLD):
                # Get old file_name
                CS.store(m_file_name.get_set(
                    None,
                    CS.current_path,
                ))
                CS.last_not_none()

        elif CS.file_operation and CS.file_operation[0] == "R":
            if CS.file_operation == "R100":
                # RENAME EXACT (content identical, reuse old fid)
                with CS(REF_OLD):
                    # Get old file_name
                    CS.store(m_file_name.get_set(
                        None,
                        CS.old_path,
                    ))
                    CS.last_not_none()

                    # Get old_bf
                    CS.store(m_bridge_file.view(
                        ((m_bridge_file.fnid, m_file_name.fnid, 1),),
                        gp.Old_VID,
                        CS.ref(m_file_name.fnid),
                        None,
                        None,
                        CS.old_path,
                    ))
                    CS.last_not_none()

                # Get new file_name
                CS.store(m_file_name.get_set(
                    None,
                    CS.current_path,
                ))

                # Create BRIDGE FILE pointing to old fid
                CS.store(m_bridge_file.set(
                    gp.VID,
                    CS.ref(m_file_name.fnid),
                    CS.ref(m_bridge_file.fid, REF_OLD),
                ))

            else:
                # RENAME MODIFY (content modified, old fid ends and new fid starts)
                with CS(REF_OLD):
                    # Get old file_name
                    CS.store(m_file_name.get_set(
                        None,
                        CS.old_path,
                    ))
                    CS.last_not_none()

                    # Get old_bf
                    CS.store(m_bridge_file.view(
                        ((m_bridge_file.fnid, m_file_name.fnid, 1),),
                        gp.Old_VID,
                        CS.ref(m_file_name.fnid),
                        None,
                        None,
                        CS.old_path,
                    ))
                    CS.last_not_none()

                # Get new file_name
                CS.store(m_file_name.get_set(
                    None,
                    CS.current_path,
                ))

                # Get FILE
                CS.store(m_file.set(
                    None,
                    getattr(CS, "blob_hash", b"\x00" * 20),
                    CS.get_file_type(),
                ))

                # Create BRIDGE FILE
                CS.store(m_bridge_file.set(
                    gp.VID,
                    CS.ref(m_file_name.fnid),
                    CS.ref(m_file.fid),
                ))

                # Create MOVED FILE
                CS.store(m_moved_file.set(
                    CS.ref(m_bridge_file.fid, REF_OLD),
                    CS.ref(m_file.fid),
                ))

        elif CS.file_operation == "M":
            # MODIFY (parsers look up the previous version's tags themselves; only renames reuse the old fid)
            # Get file_name
            CS.store(m_file_name.get_set(
                None,
                CS.current_path,
            ))
            CS.last_not_none()

            # 1 Create FILE
            CS.store(m_file.set(
                None,
                getattr(CS, "blob_hash", b"\x00" * 20),
                CS.get_file_type(),
            ))

            # 2 Create BRIDGE FILE
            CS.store(m_bridge_file.set(
                gp.VID,
                CS.ref(m_file_name.fnid),
                CS.ref(m_file.fid),
            ))

    except CONTINUE_EXCEPTION:
        logger.error(f"CONTINUE_EXCEPTION:'{CS.file_operation}'={CS.current_path}")

    # If not yet processed
    if not CS.cs:
        # Add or other
        # 0 Check if FNAME exist/Create FNAME
        CS.store(m_file_name.get_set(
            None,
            CS.current_path,
        ))

        # 1 Create FILE
        CS.store(m_file.set(
            None,
            getattr(CS, "blob_hash", b"\x00" * 20),
            CS.get_file_type(),
        ))

        # 2 Create BRIDGE FILE
        CS.store(m_bridge_file.set(
            gp.VID,
            CS.ref(m_file_name.fnid),
            CS.ref(m_file.fid),
        ))
    return


def _attach_blob_hash(CS: ChangeSet, blob_hashes: dict[str, bytes] | None) -> bool:
    """Set `CS.blob_hash` from the version's `git ls-tree` hashes.

    Returns:
        False when the version's hashes are known but the file, which needs one (it creates an m_file_blob
        row), has none, e.g. a submodule gitlink: parsing it would file it under the all-zero hash shared
        by every such file.
    """
    if not blob_hashes:
        return True
    blob_hash = blob_hashes.get(CS.current_path)
    if blob_hash is not None:
        CS.blob_hash = blob_hash
        return True
    return CS.file_operation in ("D", "R100")


def file_processing_worker(
    conn: Any,
    vid: int,
    gp_ref: GreatProcessor,
    mf_ref: MasterFile,
    worker_id: int = 0,
) -> None:
    """Worker process that receives batches of changed files on `conn` and parses them into ChangeSets.

    A serializer thread pickles each finished batch (zlib-compressed in low memory modes) together with
    its per-file errors and sends it back on `conn` as one result frame, so parsing the next batch
    (already queued by the parent) overlaps sending the last one. `send_bytes` blocks while the parent is
    behind, which caps worker memory at the batches in flight. A done frame is sent last; closing the
    pipe without it marks the worker as crashed.
    """
    sys.setrecursionlimit(50000)
    # Detach worker gp from large parent-inherited caches to prevent Copy-on-Write page duplication
    if gp_ref is not None:
        gp_ref = copy.copy(gp_ref)
        gp_ref.ChangeSet_Dict = {}
        gp_ref._ast_staged_symbols = {}
        gp_ref._ast_staged_names = {}
        gp_ref.unchanged_symbol_cache = {}
        # The parent's blob_symbol_cache is only read; what this worker loads goes back with its done frame
        gp_ref._blob_symbols_fetched = {}
        gp_ref._blob_symbols_used = set()
        gp_ref.file_symbols = {}
        gp_ref.file_names = {}
        gp_ref._ast_negative_cache = set()
        # This TableEngine is never committed: resolve_ref leaves stubs to the parent
        gp_ref._is_worker = True

    send_lock = threading.Lock()

    def _send(frame: bytes | memoryview) -> None:
        with send_lock:
            conn.send_bytes(frame)

    # Dedicated DB connection per worker process to avoid socket sharing across fork
    try:
        G.TE.start_new_db(G.DB, is_worker=True)
    except Exception as e:
        logger.error(COLOR.red(f"Worker {worker_id} failed to initialize DB connection: {e}"))
        try:
            _send(_FRAME_INIT_FAILED + pickle.dumps((f"Worker {worker_id}", f"DB connection failed: {e}", traceback.format_exc())))
        except OSError:
            pass
        conn.close()
        return

    # File-scoped shared symbol segments inherited from the parent (None during Stage 1)
    shared_symbols = getattr(gp_ref, "_shared_symbols", None) if gp_ref is not None else None
    compress = G.LOW_MEMORY_MODE
    worker_serialize_queue: queue.Queue = queue.Queue(maxsize=2)
    # Time split sent to the parent with the done frame (keys: _WORKER_SPLIT_KEYS); the serializer thread
    # keeps its own counter so the two threads never update the same entry
    split = dict.fromkeys(_WORKER_SPLIT_KEYS, 0.0)
    encode_s = [0.0]
    reads_base = (getattr(G.TE, "db_reads", 0), getattr(G.TE, "db_read_s", 0.0))
    symbols_base = Counter(SYMBOL_STATS)

    def _serializer_loop() -> None:
        while True:
            item = worker_serialize_queue.get()
            if item is None:
                break
            if len(item) == 5:
                b_id, b_files, b_dict, b_errors, b_blob_symbols = item
            else:
                b_id, b_files, b_dict, b_errors = item
                b_blob_symbols = None
            del item
            t_encode = time.perf_counter()
            try:
                frame = _encode_batch_result(b_id, b_dict, b_errors, compress, b_blob_symbols)
            except Exception as e:
                logger.error(COLOR.red(f"Worker {worker_id} failed to serialize batch {b_id}: {e}"))
                tb_str = traceback.format_exc()
                failed = [(f, f"Batch {b_id} could not be serialized: {e}", tb_str) for f in b_files]
                frame = _encode_batch_result(b_id, {}, b_errors + failed, compress)
            encode_s[0] += time.perf_counter() - t_encode
            del b_dict
            try:
                _send(frame)
            except OSError:
                # The parent stopped this stage and will terminate the worker
                return
            del frame

    serializer_thread = threading.Thread(target=_serializer_loop, daemon=True)
    serializer_thread.start()

    last_sent_blobs: set[int] = set()

    try:
        while True:
            t_wait = time.perf_counter()
            task = _recv_task(conn)
            t_task = time.perf_counter()
            split["task_wait_s"] += t_task - t_wait
            if task is None:
                # Stop frame (or the parent went away)
                break

            batch_id, changed_files = task
            batch_cs_dict = {}
            batch_errors: list[tuple[str, str, str]] = []

            # Batch pre-fetch prior tags & items for modified files in this batch
            batch_prior_tags: dict[int, list[tuple[Any, ...]]] = {}
            batch_prior_items: dict[int, tuple[Counter, Counter]] = {}
            old_vid = getattr(gp_ref, "Old_VID", 0) if gp_ref is not None else 0
            if old_vid > 0:
                try:
                    old_fids: dict[str, int] = {}
                    for cf in changed_files:
                        p = cf.split("\t")
                        if not p[0].startswith(("D", "A")) and p[0] != "R100":
                            lp = p[1] if (p[0].startswith("R") and len(p) >= 3) else p[-1]
                            fn_row = m_file_name.get(None, lp)
                            if fn_row and len(fn_row) >= 3 and fn_row[2]:
                                fnid = fn_row[2][0]
                                bf_row = m_bridge_file.get(old_vid, fnid, None)
                                if bf_row and len(bf_row) >= 3 and bf_row[2]:
                                    old_fids[lp] = bf_row[2][2]
                    if old_fids:
                        all_tag_ids: set[int] = set()
                        # One chunked read for the batch; every fid gets an entry so an empty one is never re-queried
                        batch_prior_tags.update(query_prior_tags_many(G.TE.db, set(old_fids.values())))
                        for p_tags in batch_prior_tags.values():
                            for t in p_tags:
                                tid = t[1] if len(t) > 1 else t[0]
                                if tid:
                                    all_tag_ids.add(tid)
                        if all_tag_ids:
                            tag_id_list = sorted(all_tag_ids)
                            ref_rows = G.TE.get_in(m_tag_ref.table_id, 0, tag_id_list) or ()
                            def_rows = G.TE.get_in(m_tag_def.table_id, 0, tag_id_list) or ()
                            refs_map: dict[int, Counter] = {t: Counter() for t in all_tag_ids}
                            defs_map: dict[int, Counter] = {t: Counter() for t in all_tag_ids}
                            for tag_id, _idx, key, role, rel_line, char_s in ref_rows:
                                refs_map.setdefault(tag_id, Counter())[(key, role, rel_line, char_s)] += 1
                            for tag_id, _idx, key, name, type_id, linkage, rel_s, rel_e in def_rows:
                                decoded_name = name.decode("utf-8", "replace") if isinstance(name, (bytes, bytearray)) else name
                                defs_map.setdefault(tag_id, Counter())[(key, decoded_name, type_id, linkage, rel_s, rel_e)] += 1
                            for tid in all_tag_ids:
                                batch_prior_items[tid] = (refs_map[tid], defs_map[tid])
                except Exception as e:
                    logger.debug(f"Worker {worker_id} batch prefetch prior tags failed: {e}")

            parsed_batch: list[tuple[str, ChangeSet]] = []
            for f_idx, changed_file in enumerate(changed_files):
                try:
                    CS = ChangeSet(changed_file)
                    CS.current_vid = vid
                    CS.gp = gp_ref
                    CS.mf = mf_ref
                    CS.batch_cs_dict = batch_cs_dict
                    CS._batch_prior_tags = batch_prior_tags
                    CS._batch_prior_items = batch_prior_items
                    if not _attach_blob_hash(CS, getattr(gp_ref, "file_blob_hashes", None)):
                        batch_errors.append((
                            changed_file, "No git blob hash in this version (not a regular file?); skipped.", ""
                        ))
                        continue
                    G.CURRENT_PARSING_FILE = CS.current_path

                    default_processing(CS)
                    CS.parse()
                    # Only parsing reads the batch-wide prior-tag data; it must not ride on the result frame
                    CS._batch_prior_tags = CS._batch_prior_items = None
                    if CS.file_error:
                        batch_errors.append((changed_file, CS.file_error, ""))
                    parsed_batch.append((changed_file, CS))
                except Exception as e:
                    err_str = str(e)
                    logger.error(COLOR.red(f"Worker {worker_id} error parsing '{changed_file}': {err_str}"))
                    batch_errors.append((changed_file, err_str, traceback.format_exc()))
                finally:
                    G.CURRENT_PARSING_FILE = None

                # Periodic memory reclamation in low/very-low memory modes
                if G.LOW_MEMORY_MODE and (f_idx + 1) % 25 == 0:
                    reclaim_system_memory()

            # Bulk-load uncached unchanged header symbol maps across the parsed batch before pre-resolving (O-06)
            if parsed_batch and gp_ref is not None and hasattr(gp_ref, "get_files_symbols_from_db_many"):
                changed_set = getattr(gp_ref, "_changed_paths_set", ())
                completed_set = getattr(gp_ref, "completed_paths", ())
                in_committed_stage = shared_symbols is not None
                uncached_deps: set[str] = set()
                for _, cs in parsed_batch:
                    for dep in getattr(cs, "foreign_deps", ()) or ():
                        if dep in gp_ref.unchanged_symbol_cache:
                            continue
                        if dep in changed_set and not (in_committed_stage and dep in completed_set):
                            continue
                        uncached_deps.add(dep)
                if uncached_deps:
                    try:
                        gp_ref.get_files_symbols_from_db_many(uncached_deps, version=vid)
                    except Exception as e:
                        logger.debug(f"Worker {worker_id} bulk header symbol preload failed: {e}")

            for changed_file, CS in parsed_batch:
                try:
                    G.CURRENT_PARSING_FILE = CS.current_path

                    # Pre-unpack intra-file AST view schemas in parallel workers to distribute CPU load
                    t_phase = time.perf_counter()
                    if not G.VERY_LOW_MEMORY_MODE:
                        CS.preprocess_ref_views()
                    t_views = time.perf_counter()
                    split["ref_views_s"] += t_views - t_phase

                    # Pre-resolve foreign references against symbols published by earlier stages and epochs
                    if shared_symbols:
                        CS.pre_resolve_operations(shared_symbols)
                    t_resolve = time.perf_counter()
                    split["pre_resolve_s"] += t_resolve - t_views

                    # In normal memory mode, pre-resolve unchanged DB dependencies to prune foreign_deps
                    # (Stage 2 only: pre_resolve_operations above did the same lookups and trimmed foreign_deps)
                    if not G.LOW_MEMORY_MODE and not shared_symbols:
                        CS.prune_unchanged_dependencies()
                    split["prune_s"] += time.perf_counter() - t_resolve

                    # Clean bloat and store in batch dict
                    CS.clear_bloat()
                    batch_cs_dict[CS.current_path] = CS
                except Exception as e:
                    err_str = str(e)
                    logger.error(COLOR.red(f"Worker {worker_id} error parsing '{changed_file}': {err_str}"))
                    batch_errors.append((changed_file, err_str, traceback.format_exc()))
                finally:
                    G.CURRENT_PARSING_FILE = None

            t_put = time.perf_counter()
            split["busy_s"] += t_put - t_task
            new_blob_symbols = None
            if gp_ref is not None and hasattr(gp_ref, "_blob_symbols_fetched"):
                new_blob_symbols = {
                    fid: sm for fid, sm in gp_ref._blob_symbols_fetched.items()
                    if fid not in last_sent_blobs
                }
                last_sent_blobs.update(new_blob_symbols.keys())
            # Blocks while two finished batches wait for the parent to read them
            worker_serialize_queue.put((batch_id, changed_files, batch_cs_dict, batch_errors, new_blob_symbols))
            t_queued = time.perf_counter()
            split["send_wait_s"] += t_queued - t_put
            del batch_cs_dict
            reclaim_system_memory()
            split["gc_s"] += time.perf_counter() - t_queued
    finally:
        while serializer_thread.is_alive():
            try:
                worker_serialize_queue.put(None, timeout=1.0)
                break
            except queue.Full:
                continue
        serializer_thread.join()
        if getattr(G.TE, "db", None) is not None:
            try:
                G.TE.db.close()
            except Exception:
                pass
            G.TE.db = None
        reclaim_system_memory()
        split["encode_s"] = encode_s[0]
        split["db_reads"] = getattr(G.TE, "db_reads", 0) - reads_base[0]
        split["db_read_s"] = getattr(G.TE, "db_read_s", 0.0) - reads_base[1]
        split["symbol_stats"] = dict(SYMBOL_STATS - symbols_base)
        if gp_ref is not None:
            # Maps already streamed with a batch frame are not sent again
            split["blob_symbols"] = {
                fid: sm for fid, sm in (getattr(gp_ref, "_blob_symbols_fetched", None) or {}).items()
                if fid not in last_sent_blobs
            }
            split["blob_symbols_used"] = list(getattr(gp_ref, "_blob_symbols_used", ()))
        try:
            _send(_FRAME_DONE + pickle.dumps(split, protocol=pickle.HIGHEST_PROTOCOL))
        except OSError:
            pass
        conn.close()
    return


def file_processing(start: int, end: int | None, override_list: list[str] | None = None) -> None:
    """Process gp.Change_List (or override_list) and send CS into gp.ChangeSet_Dict."""
    if override_list:
        changed_files = override_list
    elif end is None:
        changed_files = gp.Change_List[start:]
    else:
        changed_files = gp.Change_List[start:end]

    for changed_file in changed_files:
        try:
            CS = ChangeSet(changed_file)
            CS.current_vid = gp.VID
            CS.gp = gp
            CS.mf = MF
            if not _attach_blob_hash(CS, getattr(gp, "file_blob_hashes", None)):
                logger.error(COLOR.red(f"Skipping '{changed_file}': no git blob hash in this version (not a regular file?)"))
                continue
            G.CURRENT_PARSING_FILE = CS.current_path

            default_processing(CS)
            CS.parse()

            # Store Set
            CS.clear_bloat()
            gp.ChangeSet_Dict[CS.current_path] = CS
        except Exception as e:
            logger.error(COLOR.red(f"Error processing file '{changed_file}': {e}\n{traceback.format_exc()}"))
        finally:
            G.CURRENT_PARSING_FILE = None

    return
