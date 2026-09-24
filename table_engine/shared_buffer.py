"""table_engine/shared_buffer.py - Huge-Page Backed Structured Shared Memory for TableEngine.

Provides:
1. Tiered opportunistic allocation (1GB HugeTLB -> 2MB HugeTLB -> 2MB THP -> 4KB standard).
2. Packed zero-dependency binary serialization for relational table rows (SafeDataType).
3. Compact in-memory open-addressing hash indices directly inside raw mmap bytes for O(1) lookups.
4. Seamless lock-free read-only sharing across forked worker processes.
"""
from __future__ import annotations

import ctypes
import hashlib
import logging
import mmap
import os
import struct
import sys
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# Linux Huge Page & madvise constants
MAP_HUGETLB = 0x40000
MAP_HUGE_2MB = 21 << 26  # 0x54000000
MAP_HUGE_1GB = 30 << 26  # 0x78000000
MADV_HUGEPAGE = 14

# Magic header & version
SHARED_BUFFER_MAGIC = b"TEHP"
SHARED_BUFFER_VERSION = 1

# Column value type tags
TAG_NONE = 0
TAG_INT = 1
TAG_STR = 2
TAG_BYTES = 3

# Hash index configuration
EMPTY_SLOT_HASH = 0
EMPTY_SLOT_OFFSET = 0xFFFFFFFFFFFFFFFF


class SharedBufferAllocator:
    """Manages memory allocation backed opportunistically by huge pages."""

    @staticmethod
    def _try_madvise_hugepage(mm: mmap.mmap, size: int) -> bool:
        """Attempt to mark an anonymous mmap with MADV_HUGEPAGE for 2MB THP backing."""
        try:
            libc = ctypes.CDLL(None)
            buf_ptr = ctypes.c_void_p(ctypes.addressof(ctypes.c_char.from_buffer(mm)))
            res = libc.madvise(buf_ptr, ctypes.c_size_t(size), MADV_HUGEPAGE)
            return res == 0
        except Exception:
            return False

    @classmethod
    def allocate(cls, size: int, mode: str = "auto") -> tuple[mmap.mmap, str, int]:
        """Allocate a shared memory mapping adhering to mode preference with graceful fallbacks.

        Modes:
            - '1g': Require/attempt 1GB HugeTLB.
            - '2m': Require/attempt 2MB HugeTLB.
            - 'thp': Attempt 2MB Transparent Huge Pages via madvise.
            - 'auto': Attempt 1GB -> 2MB -> THP -> 4KB.
            - 'off': Standard 4KB anonymous shared mmap.

        Returns:
            (mmap_instance, mode_used, page_size_bytes)
        """
        mode = mode.lower().strip()
        if mode not in ("auto", "1g", "2m", "thp", "off"):
            mode = "auto"

        # Round size up to at least 2MB boundary for huge page compatibility
        min_align = 2 * 1024 * 1024
        aligned_size = ((size + min_align - 1) // min_align) * min_align
        aligned_size = max(aligned_size, min_align)

        # 1. Attempt 1GB Huge Pages
        if mode in ("auto", "1g") and sys.platform.startswith("linux"):
            one_gb_align = 1024 * 1024 * 1024
            sz_1g = ((aligned_size + one_gb_align - 1) // one_gb_align) * one_gb_align
            try:
                mm = mmap.mmap(
                    -1,
                    sz_1g,
                    flags=mmap.MAP_SHARED | mmap.MAP_ANONYMOUS | MAP_HUGETLB | MAP_HUGE_1GB,
                    prot=mmap.PROT_READ | mmap.PROT_WRITE,
                )
                logger.info(f"[SharedBuffer] Allocated {sz_1g // (1024*1024)}MB backed by 1GB HugeTLB pages.")
                return mm, "1g", one_gb_align
            except (OSError, ValueError, PermissionError) as exc:
                if mode == "1g":
                    logger.warning(f"[SharedBuffer] 1GB HugeTLB requested but allocation failed: {exc}. Falling back to 2MB.")

        # 2. Attempt 2MB HugeTLB
        if mode in ("auto", "1g", "2m") and sys.platform.startswith("linux"):
            try:
                mm = mmap.mmap(
                    -1,
                    aligned_size,
                    flags=mmap.MAP_SHARED | mmap.MAP_ANONYMOUS | MAP_HUGETLB | MAP_HUGE_2MB,
                    prot=mmap.PROT_READ | mmap.PROT_WRITE,
                )
                logger.info(f"[SharedBuffer] Allocated {aligned_size // (1024*1024)}MB backed by 2MB HugeTLB pages.")
                return mm, "2m", 2 * 1024 * 1024
            except (OSError, ValueError, PermissionError) as exc:
                if mode == "2m":
                    logger.warning(f"[SharedBuffer] 2MB HugeTLB requested but allocation failed: {exc}. Falling back to THP.")

        # 3. Attempt 2MB Transparent Huge Pages (THP) via madvise
        if mode in ("auto", "1g", "2m", "thp") and sys.platform.startswith("linux"):
            try:
                mm = mmap.mmap(
                    -1,
                    aligned_size,
                    flags=mmap.MAP_SHARED | mmap.MAP_ANONYMOUS,
                    prot=mmap.PROT_READ | mmap.PROT_WRITE,
                )
                if cls._try_madvise_hugepage(mm, aligned_size):
                    logger.info(f"[SharedBuffer] Allocated {aligned_size // (1024*1024)}MB backed by 2MB Transparent Huge Pages (MADV_HUGEPAGE).")
                    return mm, "thp", 2 * 1024 * 1024
                else:
                    logger.info(f"[SharedBuffer] Allocated {aligned_size // (1024*1024)}MB anonymous shared memory (THP madvise not active).")
                    return mm, "4k", 4096
            except (OSError, ValueError) as exc:
                logger.warning(f"[SharedBuffer] Anonymous shared mmap failed: {exc}. Falling back to 4KB.")

        # 4. Standard 4KB anonymous shared mmap
        mm = mmap.mmap(
            -1,
            aligned_size,
            flags=mmap.MAP_SHARED | mmap.MAP_ANONYMOUS,
            prot=mmap.PROT_READ | mmap.PROT_WRITE,
        )
        logger.info(f"[SharedBuffer] Allocated {aligned_size // (1024*1024)}MB backed by standard 4KB pages.")
        return mm, "4k", 4096


def _hash64(val: Any) -> int:
    """Compute a deterministic non-zero 64-bit hash for open-addressing slot indexing."""
    if isinstance(val, int):
        h = (val * 0x517CC1B727220A95) & 0xFFFFFFFFFFFFFFFF
    elif isinstance(val, (bytes, bytearray, memoryview)):
        raw = bytes(val)
        if len(raw) >= 8:
            h = struct.unpack("<Q", raw[:8])[0]
        else:
            h = int.from_bytes(hashlib.sha256(raw).digest()[:8], "little")
    elif isinstance(val, tuple):
        h = int.from_bytes(hashlib.sha256(repr(val).encode("utf-8")).digest()[:8], "little")
    else:
        h = int.from_bytes(hashlib.sha256(str(val).encode("utf-8")).digest()[:8], "little")
    return h if h != EMPTY_SLOT_HASH else 1


class SharedTableReader:
    """Zero-copy fast reader querying structured tables directly inside shared huge page mmap."""

    def __init__(self, mm: mmap.mmap) -> None:
        self.mm = mm
        self.mv = memoryview(mm)
        self.tables: dict[int, dict[str, Any]] = {}
        self._load_header()

    def _load_header(self) -> None:
        """Parse directory header and initialize table descriptor metadata."""
        if len(self.mv) < 64:
            return
        magic = bytes(self.mv[0:4])
        if magic != SHARED_BUFFER_MAGIC:
            return
        version, page_size, total_size, num_tables, dir_offset = struct.unpack_from(
            "<IIQQQ", self.mv, 4
        )
        self.version = version
        self.page_size = page_size
        self.total_size = total_size

        offset = dir_offset
        for _ in range(num_tables):
            (
                t_id,
                row_count,
                col_count,
                pk_len,
                pk0, pk1, pk2, pk3,
                idx_type,
                idx_offset,
                idx_cap,
                data_offset,
                data_size,
                str_offset,
                str_size,
            ) = struct.unpack_from("<IQQH4HHQQQQQQ", self.mv, offset)
            offset += struct.calcsize("<IQQH4HHQQQQQQ")

            pk_tuple = tuple(c for c in (pk0, pk1, pk2, pk3)[:pk_len] if c != 0xFFFF)
            self.tables[t_id] = {
                "row_count": row_count,
                "col_count": col_count,
                "primary": pk_tuple,
                "idx_type": idx_type,
                "idx_offset": idx_offset,
                "idx_cap": idx_cap,
                "data_offset": data_offset,
                "data_size": data_size,
                "str_offset": str_offset,
                "str_size": str_size,
            }

    def contains_table(self, table_id: int) -> bool:
        return table_id in self.tables

    def get_row_by_pk(self, table_id: int, pk_val: Any) -> tuple | None:
        """O(1) Primary Key lookup probing the open-addressing hash table in shared memory."""
        t_meta = self.tables.get(table_id)
        if not t_meta or t_meta["row_count"] == 0:
            return None

        cap = t_meta["idx_cap"]
        mask = cap - 1
        h64 = _hash64(pk_val)
        idx_base = t_meta["idx_offset"]

        step = 1
        slot = h64 & mask
        while True:
            slot_offset = idx_base + slot * 16
            k_hash, rec_off = struct.unpack_from("<QQ", self.mv, slot_offset)
            if rec_off == EMPTY_SLOT_OFFSET:
                return None
            if k_hash == h64:
                # Potential match - unpack row and verify PK matches exactly
                row = self._unpack_row(t_meta, rec_off)
                pk_cols = t_meta["primary"]
                if len(pk_cols) == 1:
                    actual_pk = row[pk_cols[0]]
                else:
                    actual_pk = tuple(row[c] for c in pk_cols)
                if actual_pk == pk_val:
                    return row

            slot = (slot + step) & mask
            step += 1
            if step > cap:
                return None

    def _unpack_row(self, t_meta: dict[str, Any], rec_off: int) -> tuple:
        """Reconstruct tuple of SafeDataType from slotted record in shared memory."""
        col_count = t_meta["col_count"]
        str_base = t_meta["str_offset"]
        cells = []
        cur = rec_off
        for _ in range(col_count):
            tag = self.mv[cur]
            cur += 1
            if tag == TAG_NONE:
                cells.append(None)
                cur += 8
            elif tag == TAG_INT:
                val = struct.unpack_from("<q", self.mv, cur)[0]
                cells.append(val)
                cur += 8
            elif tag == TAG_STR:
                s_off, s_len = struct.unpack_from("<II", self.mv, cur)
                cur += 8
                s_bytes = bytes(self.mv[str_base + s_off : str_base + s_off + s_len])
                cells.append(s_bytes.decode("utf-8", errors="replace"))
            elif tag == TAG_BYTES:
                s_off, s_len = struct.unpack_from("<II", self.mv, cur)
                cur += 8
                cells.append(bytes(self.mv[str_base + s_off : str_base + s_off + s_len]))
            else:
                cells.append(None)
                cur += 8
        return tuple(cells)

    def scan_matching(self, table_id: int, filter_cols: tuple) -> tuple | None:
        """Linear scan of shared table rows matching filter criteria."""
        t_meta = self.tables.get(table_id)
        if not t_meta or t_meta["row_count"] == 0:
            return None
        col_count = t_meta["col_count"]
        rec_size = col_count * 9
        data_base = t_meta["data_offset"]
        for i in range(t_meta["row_count"]):
            rec_off = data_base + i * rec_size
            row = self._unpack_row(t_meta, rec_off)
            match = True
            for f_val, r_val in zip(filter_cols, row):
                if f_val is not None and f_val != r_val:
                    match = False
                    break
            if match:
                return row
        return None


class SharedTableBuilder:
    """Packs preloaded relational tables and builds open-addressing hash indices into a shared mmap."""

    def __init__(self, mode: str = "auto") -> None:
        self.mode = mode
        self.table_data: dict[int, dict[str, Any]] = {}

    def add_table(self, table_id: int, col_count: int, primary: tuple[int, ...], rows: Sequence[tuple]) -> None:
        """Register preloaded table rows for serialization into the shared buffer."""
        self.table_data[table_id] = {
            "col_count": col_count,
            "primary": primary,
            "rows": list(rows),
        }

    def build(self) -> SharedTableReader | None:
        """Serialize registered tables and construct open-addressing indices into shared huge page mmap."""
        if not self.table_data:
            return None

        # 1. Calculate required capacity
        HEADER_SIZE = 64
        DIR_ENTRY_SIZE = struct.calcsize("<IQQH4HHQQQQQQ")
        num_tables = len(self.table_data)
        dir_size = num_tables * DIR_ENTRY_SIZE

        total_needed = HEADER_SIZE + dir_size
        table_plans = {}

        for t_id, t_info in self.table_data.items():
            rows = t_info["rows"]
            col_count = t_info["col_count"]
            row_count = len(rows)

            # Record size: 9 bytes per cell (1 tag + 8 data)
            rec_size = col_count * 9
            data_size = row_count * rec_size

            # Index size: power-of-two capacity >= 2 * row_count
            cap = 16
            while cap < max(16, row_count * 2):
                cap *= 2
            idx_size = cap * 16  # 16 bytes per slot

            # Estimate string/bytes pool
            pool_bytes = 0
            for r in rows:
                for c in r:
                    if isinstance(c, str):
                        pool_bytes += len(c.encode("utf-8"))
                    elif isinstance(c, (bytes, bytearray, memoryview)):
                        pool_bytes += len(c)

            table_plans[t_id] = {
                "row_count": row_count,
                "col_count": col_count,
                "primary": t_info["primary"],
                "data_size": data_size,
                "idx_cap": cap,
                "idx_size": idx_size,
                "pool_bytes": pool_bytes,
                "rows": rows,
            }
            total_needed += data_size + idx_size + pool_bytes + 1024  # padding

        # 2. Allocate shared mmap
        mm, mode_used, page_sz = SharedBufferAllocator.allocate(total_needed, mode=self.mode)
        mv = memoryview(mm)

        # 3. Layout planning and writing
        dir_offset = HEADER_SIZE
        current_offset = HEADER_SIZE + dir_size

        table_dir_records = []
        for t_id, plan in table_plans.items():
            col_count = plan["col_count"]
            rows = plan["rows"]
            primary = plan["primary"]
            cap = plan["idx_cap"]
            row_count = plan["row_count"]

            # Layout regions for this table
            idx_offset = current_offset
            current_offset += plan["idx_size"]

            data_offset = current_offset
            current_offset += plan["data_size"]

            str_offset = current_offset
            # Populate index with empty sentinels
            for slot_i in range(cap):
                struct.pack_into("<QQ", mv, idx_offset + slot_i * 16, EMPTY_SLOT_HASH, EMPTY_SLOT_OFFSET)

            # Pack rows and strings
            pool_cur = str_offset
            rec_size = col_count * 9
            mask = cap - 1

            for r_idx, row in enumerate(rows):
                rec_off = data_offset + r_idx * rec_size
                cell_cur = rec_off
                for col_i in range(col_count):
                    val = row[col_i] if col_i < len(row) else None
                    if val is None:
                        mv[cell_cur] = TAG_NONE
                        struct.pack_into("<q", mv, cell_cur + 1, 0)
                    elif isinstance(val, int):
                        mv[cell_cur] = TAG_INT
                        struct.pack_into("<q", mv, cell_cur + 1, val)
                    elif isinstance(val, str):
                        mv[cell_cur] = TAG_STR
                        raw = val.encode("utf-8")
                        s_len = len(raw)
                        s_rel = pool_cur - str_offset
                        mv[pool_cur : pool_cur + s_len] = raw
                        pool_cur += s_len
                        struct.pack_into("<II", mv, cell_cur + 1, s_rel, s_len)
                    elif isinstance(val, (bytes, bytearray, memoryview)):
                        mv[cell_cur] = TAG_BYTES
                        raw = bytes(val)
                        s_len = len(raw)
                        s_rel = pool_cur - str_offset
                        mv[pool_cur : pool_cur + s_len] = raw
                        pool_cur += s_len
                        struct.pack_into("<II", mv, cell_cur + 1, s_rel, s_len)
                    else:
                        mv[cell_cur] = TAG_NONE
                        struct.pack_into("<q", mv, cell_cur + 1, 0)
                    cell_cur += 9

                # Index row by Primary Key
                if primary:
                    if len(primary) == 1:
                        pk_val = row[primary[0]]
                    else:
                        pk_val = tuple(row[c] for c in primary)
                    if pk_val is not None:
                        h64 = _hash64(pk_val)
                        slot = h64 & mask
                        step = 1
                        while True:
                            s_off = idx_offset + slot * 16
                            k_hash, k_rec = struct.unpack_from("<QQ", mv, s_off)
                            if k_rec == EMPTY_SLOT_OFFSET:
                                struct.pack_into("<QQ", mv, s_off, h64, rec_off)
                                break
                            slot = (slot + step) & mask
                            step += 1

            actual_str_size = pool_cur - str_offset
            current_offset = pool_cur

            # Build directory entry
            pk_cols = list(primary) + [0xFFFF] * 4
            table_dir_records.append((
                t_id,
                row_count,
                col_count,
                len(primary),
                pk_cols[0], pk_cols[1], pk_cols[2], pk_cols[3],
                1,  # idx_type: PK hash index
                idx_offset,
                cap,
                data_offset,
                plan["data_size"],
                str_offset,
                actual_str_size,
            ))

        # 4. Write table directory
        d_cur = dir_offset
        for dir_rec in table_dir_records:
            struct.pack_into("<IQQH4HHQQQQQQ", mv, d_cur, *dir_rec)
            d_cur += DIR_ENTRY_SIZE

        # 5. Write header
        struct.pack_into(
            "<4sIIQQQ",
            mv,
            0,
            SHARED_BUFFER_MAGIC,
            SHARED_BUFFER_VERSION,
            page_sz,
            len(mm),
            num_tables,
            dir_offset,
        )

        return SharedTableReader(mm)
