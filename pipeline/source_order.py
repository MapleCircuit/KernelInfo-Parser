"""Ordering and epoch partitioning of the changed source files of a version."""
import os

import math
import re
from collections import deque, defaultdict
from pipeline.state import MF, gp
from pipeline.worker_stage import _BATCHES_PER_WORKER




_INCLUDE_REGEX = re.compile(r'^\s*#\s*include\s*["<]([^">]+)[">]', re.M)


def order_changed_files(regular_files: list[str], working_dir: str | None) -> list[str]:
    """Topologically sort changed files so header files (.h) are parsed before dependent .c/.S files."""
    if not regular_files or not working_dir:
        return regular_files

    header_items = []
    other_items = []
    header_path_to_item = {}

    for item in regular_files:
        fpath = item.split("\t")[-1]
        if fpath.endswith((".h", ".hpp", ".hxx")):
            header_items.append(item)
            header_path_to_item[fpath] = item
        else:
            other_items.append(item)

    if len(header_items) <= 1:
        return header_items + other_items

    # Build dependency graph between changed headers
    # edge A -> B means B includes A (A must be parsed before B)
    graph = defaultdict(set)
    in_degree = defaultdict(int)
    for fpath in header_path_to_item:
        in_degree[fpath] = 0

    for fpath, item in header_path_to_item.items():
        if item.startswith("D"):
            continue
        full_path = os.path.join(working_dir, fpath)
        try:
            with open(full_path, "r", encoding="latin-1", errors="ignore") as f:
                content = f.read(131072)  # Read up to 128KB
        except Exception:
            continue

        for inc_match in _INCLUDE_REGEX.finditer(content):
            inc_target = inc_match.group(1)
            target_fpath = None
            if inc_target in header_path_to_item:
                target_fpath = inc_target
            elif f"include/{inc_target}" in header_path_to_item:
                target_fpath = f"include/{inc_target}"
            else:
                rel_candidate = os.path.normpath(os.path.join(os.path.dirname(fpath), inc_target))
                if rel_candidate in header_path_to_item:
                    target_fpath = rel_candidate

            if target_fpath and target_fpath != fpath:
                if fpath not in graph[target_fpath]:
                    graph[target_fpath].add(fpath)
                    in_degree[fpath] += 1

    # Kahn's algorithm for topological sort
    zero_in = deque([f for f, deg in in_degree.items() if deg == 0])
    sorted_header_paths = []

    while zero_in:
        node = zero_in.popleft()
        sorted_header_paths.append(node)
        for neighbor in graph[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                zero_in.append(neighbor)

    # If cyclic dependencies exist, append remaining headers in their original order
    if len(sorted_header_paths) < len(header_path_to_item):
        seen = set(sorted_header_paths)
        for item in header_items:
            fpath = item.split("\t")[-1]
            if fpath not in seen:
                sorted_header_paths.append(fpath)
                seen.add(fpath)

    sorted_headers = [header_path_to_item[fpath] for fpath in sorted_header_paths]
    return sorted_headers + other_items


_AST_SOURCE_EXTS = frozenset((
    ".c", ".cpp", ".cc", ".cxx", ".c++", ".cp",
    ".rs",
    ".s", ".asm",
    ".h", ".hpp", ".hxx",
))


def _is_ast_source_path(fpath: str) -> bool:
    """Return True if `fpath` is in a language parsed into a full AST (C/C++, Rust, assembly)."""
    return os.path.splitext(fpath)[1].lower() in _AST_SOURCE_EXTS


def is_heavy_source_file(
    fpath: str,
    working_dir: str | None = None,
    threshold_bytes: int = 65536,
) -> bool:
    """Check if file has high parsing complexity in a project-agnostic manner.

    Determines weight based on file size and language complexity rather than hardcoded directory paths.
    Compiled AST files (.c, .cpp, .cc, .cxx, .rs, .s, .asm) exceeding threshold_bytes (default 64KB)
    are classified as heavy.

    Args:
        fpath: Relative or absolute file path (or git diff tab-delimited item).
        working_dir: Optional root working directory for disk stat lookups.
        threshold_bytes: Byte size threshold above which AST files are considered heavy.

    Returns:
        True if the file is an AST source file exceeding threshold_bytes, False otherwise.
    """
    if "\t" in fpath:
        fpath = fpath.split("\t")[-1]

    if not _is_ast_source_path(fpath):
        return False

    if working_dir is None:
        try:
            if hasattr(gp, "Version_Name") and gp.Version_Name:
                working_dir = MF.version_dict.get(gp.Version_Name)
        except Exception:
            pass

    full_path = os.path.join(working_dir, fpath) if (working_dir and not os.path.isabs(fpath)) else fpath
    try:
        if os.path.exists(full_path):
            return os.path.getsize(full_path) >= threshold_bytes
    except Exception:
        pass

    return False


# Backward compatibility alias
is_heavy_source_path = is_heavy_source_file


def partition_source_epochs(
    source_files: list[str],
    working_dir: str | None = None,
    normal_batch_size: int = 200,
    heavy_batch_size: int = 50,
    max_batch_bytes: int = 1500000,
    heavy_file_threshold: int = 65536,
    target_epoch_files: int = 4500,
    largest_first: bool = True,
    num_workers: int = 0,
    target_epoch_bytes: int | None = None,
) -> list[list[tuple[int, list[str]]]]:
    """Partition source files into batches with dynamic complexity sizing and group them into natural subsystem-aligned epochs.

    Uses project-agnostic heuristics applicable to any repository:
    1. File complexity weight: Measures file size and language type rather than fixed directory rules.
    2. Dual-budget batching: Batches are bounded by both file count and cumulative source byte weight.
    3. Natural directory transitions: Epoch boundaries align with top-level directory changes.

    Args:
        source_files: List of git diff items for source files in preserved order.
        working_dir: Optional root working directory for disk stat lookups.
        normal_batch_size: Default batch size for lightweight files (e.g. 200).
        heavy_batch_size: Reduced batch size when heavy files are present (e.g. 50).
        max_batch_bytes: Maximum cumulative source code bytes per batch (e.g. 1.5MB).
        heavy_file_threshold: File size threshold (bytes) to flag an AST file as heavy.
        target_epoch_files: Approximate target count of files per epoch (e.g. 4500).
        largest_first: Order each epoch's batches by descending source bytes, so the heaviest batches
            start first and the epoch does not end waiting on one late heavy batch.
        num_workers: When given and the files fit in one epoch, the byte budget per batch is also capped at an
            equal share of the bytes (`_BATCHES_PER_WORKER` batches per worker), so batches cost about the same.

    Returns:
        List of epochs, where each epoch is a list of (global_batch_id, batch_files) tuples.
    """
    if not source_files:
        return []

    if working_dir is None:
        try:
            if hasattr(gp, "Version_Name") and gp.Version_Name:
                working_dir = MF.version_dict.get(gp.Version_Name)
        except Exception:
            pass

    sizes: list[int] = []
    for item in source_files:
        fpath = item.split("\t")[-1]
        sz = 0
        if working_dir:
            full_path = os.path.join(working_dir, fpath) if not os.path.isabs(fpath) else fpath
            try:
                sz = os.path.getsize(full_path)
            except Exception:
                sz = 0
        sizes.append(sz)
    total_bytes = sum(sizes)
    if target_epoch_bytes is not None and total_bytes > 0:
        single_epoch = total_bytes <= int(target_epoch_bytes * 1.3)
    else:
        single_epoch = len(source_files) <= int(target_epoch_files * 1.3)
        if not single_epoch and total_bytes > 0:
            target_epoch_bytes = max(1, int(total_bytes * target_epoch_files / len(source_files)))
    if num_workers > 0 and single_epoch:
        equal_share = math.ceil(total_bytes / (num_workers * _BATCHES_PER_WORKER))
        max_batch_bytes = min(max_batch_bytes, max(heavy_file_threshold, equal_share))

    batches: list[tuple[int, list[str]]] = []
    batch_bytes: dict[int, int] = {}
    current_batch: list[str] = []
    current_batch_bytes = 0
    current_has_heavy = False
    batch_counter = 0

    for item, sz in zip(source_files, sizes):
        fpath = item.split("\t")[-1]

        is_heavy = sz >= heavy_file_threshold and _is_ast_source_path(fpath)
        limit = heavy_batch_size if (current_has_heavy or is_heavy) else normal_batch_size

        if len(current_batch) >= limit or (current_batch_bytes + sz > max_batch_bytes and len(current_batch) > 0):
            if current_batch:
                batches.append((batch_counter, current_batch))
                batch_bytes[batch_counter] = current_batch_bytes
                batch_counter += 1
                current_batch = []
                current_batch_bytes = 0
                current_has_heavy = False

        current_batch.append(item)
        current_batch_bytes += sz
        if is_heavy:
            current_has_heavy = True

    if current_batch:
        batches.append((batch_counter, current_batch))
        batch_bytes[batch_counter] = current_batch_bytes
        batch_counter += 1

    def _ordered(epoch: list[tuple[int, list[str]]]) -> list[tuple[int, list[str]]]:
        if not largest_first:
            return epoch
        return sorted(epoch, key=lambda batch: batch_bytes[batch[0]], reverse=True)

    if single_epoch:
        return [_ordered(batches)]

    use_bytes = total_bytes > 0 and target_epoch_bytes is not None and target_epoch_bytes > 0
    epochs: list[list[tuple[int, list[str]]]] = []
    current_epoch: list[tuple[int, list[str]]] = []
    current_epoch_files = 0
    current_epoch_bytes = 0

    for idx, (b_id, b_files) in enumerate(batches):
        current_epoch.append((b_id, b_files))
        current_epoch_files += len(b_files)
        current_epoch_bytes += batch_bytes[b_id]

        threshold_reached = (
            current_epoch_bytes >= target_epoch_bytes
            if use_bytes
            else current_epoch_files >= target_epoch_files
        )
        if threshold_reached and idx + 1 < len(batches):
            curr_f = current_epoch[-1][1][-1].split("\t")[-1]
            next_f = batches[idx + 1][1][0].split("\t")[-1]
            curr_parts = os.path.dirname(curr_f).split(os.sep)
            next_parts = os.path.dirname(next_f).split(os.sep)
            curr_dir = os.sep.join(curr_parts[:2]) if len(curr_parts) > 1 else (curr_parts[0] if curr_parts else "")
            next_dir = os.sep.join(next_parts[:2]) if len(next_parts) > 1 else (next_parts[0] if next_parts else "")

            hard_cap = (
                current_epoch_bytes >= int(target_epoch_bytes * 1.35)
                if use_bytes
                else current_epoch_files >= int(target_epoch_files * 1.35)
            )
            if curr_dir != next_dir or hard_cap:
                epochs.append(_ordered(current_epoch))
                current_epoch = []
                current_epoch_files = 0
                current_epoch_bytes = 0

    if current_epoch:
        epochs.append(_ordered(current_epoch))

    return epochs
