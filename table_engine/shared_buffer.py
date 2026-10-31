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
    def allocate(cls, size: int, mode: str = "auto", allow_1g: bool = True) -> tuple[mmap.mmap, str, int]:
        """Allocate a shared memory mapping adhering to mode preference with graceful fallbacks.

        `allow_1g=False` skips 1GB HugeTLB pages (for mappings far smaller than a 1GB page) while keeping the
        quiet fallbacks of `auto`.

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
        if mode in ("auto", "1g") and allow_1g and sys.platform.startswith("linux"):
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
        # Fast SplitMix64 / FxHash tuple mixing for composite primary keys
        h = 0xCBF29CE484222325
        for item in val:
            if isinstance(item, int):
                ih = (item * 0x517CC1B727220A95) & 0xFFFFFFFFFFFFFFFF
            elif isinstance(item, (bytes, bytearray, memoryview)):
                raw = bytes(item)
                if len(raw) >= 8:
                    ih = struct.unpack("<Q", raw[:8])[0]
                else:
                    ih = int.from_bytes(hashlib.sha256(raw).digest()[:8], "little")
            else:
                ih = _hash64(item)
            h = ((h ^ ih) * 0xBF58476D1CE4E5B9 + 0x517CC1B727220A95) & 0xFFFFFFFFFFFFFFFF
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

    # A segment smaller than this wastes most of a 1GB huge page, so it is backed by 2MB pages instead
    MIN_1G_BYTES = 512 * 1024 * 1024

    def __init__(self, mode: str = "auto") -> None:
        self.mode = mode
        self.table_data: dict[int, dict[str, Any]] = {}

    def add_table(self, table_id: int, col_count: int, primary: tuple[int, ...], rows: Sequence[tuple]) -> None:
        """Register preloaded table rows for serialization into the shared buffer.

        The rows are referenced, not copied: a caller that drops its own reference leaves them to this builder.
        """
        row_list = rows if isinstance(rows, list) else list(rows)
        self.table_data[table_id] = {
            "col_count": col_count,
            "primary": primary,
            "rows": row_list,
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

        # 2. Allocate shared mmap (a segment far below a 1GB page is backed by 2MB pages instead)
        mm, mode_used, page_sz = SharedBufferAllocator.allocate(
            total_needed, mode=self.mode, allow_1g=total_needed >= self.MIN_1G_BYTES
        )
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

        # The rows are packed: drop the builder's hold on them so the heap copy can be freed at once
        self.table_data.clear()
        table_plans.clear()

        return SharedTableReader(mm)


# Rows measured per table when estimating a batch's packed size (`estimate_packed_bytes`)
_ESTIMATE_SAMPLE = 64


def estimate_packed_bytes(col_count: int, rows: Sequence[tuple]) -> int:
    """Estimate the shared-memory bytes `rows` will take, from a sample of them.

    Used to decide when a preload batch is large enough to pack (`TECachedDB.start_new_db()`).
    Measuring every cell of every table would cost about as much as the packing it schedules, so the
    variable-length share is averaged over at most `_ESTIMATE_SAMPLE` rows spread across the table.
    """
    row_count = len(rows)
    if row_count == 0:
        return 0
    step = max(1, row_count // _ESTIMATE_SAMPLE)
    sampled = 0
    var_bytes = 0
    for i in range(0, row_count, step):
        sampled += 1
        for val in rows[i]:
            if isinstance(val, str):
                var_bytes += len(val)
            elif isinstance(val, (bytes, bytearray, memoryview)):
                var_bytes += len(val)
    # 9 bytes per cell, 16 bytes per index slot at a capacity of at least twice the row count
    return row_count * (col_count * 9 + 32 + var_bytes // sampled)


class SharedTableSegments:
    """Several shared-memory segments presented as one reader, each table living wholly in one of them.

    Preloading packs a segment as soon as the tables it has read reach a byte budget and then drops their
    heap rows, so the heap holds one batch at a time instead of every table at once while the packed copy
    of all of them is built (`TECachedDB.start_new_db()`).
    """

    def __init__(self) -> None:
        self.segments: list[SharedTableReader] = []
        # Which segment holds a table, and the merged table metadata of all of them
        self._owner: dict[int, SharedTableReader] = {}
        self.tables: dict[int, dict[str, Any]] = {}
        self.total_size = 0
        # The weakest page size any segment got (they are allocated one by one)
        self.page_size = 0

    def add_segment(self, reader: SharedTableReader) -> None:
        """Adopt a freshly packed segment and route its tables to it."""
        self.segments.append(reader)
        for t_id, meta in reader.tables.items():
            self._owner[t_id] = reader
            self.tables[t_id] = meta
        self.total_size += getattr(reader, "total_size", 0) or 0
        page_sz = getattr(reader, "page_size", 0) or 0
        self.page_size = min(self.page_size, page_sz) if self.page_size else page_sz

    def reader_for(self, table_id: int) -> SharedTableReader | None:
        """The segment holding this table, which its zero-copy views are bound to."""
        return self._owner.get(table_id)

    def contains_table(self, table_id: int) -> bool:
        return table_id in self._owner

    def get_row_by_pk(self, table_id: int, pk_val: Any) -> tuple | None:
        """O(1) Primary Key lookup in the segment holding the table."""
        reader = self._owner.get(table_id)
        return reader.get_row_by_pk(table_id, pk_val) if reader is not None else None

    def scan_matching(self, table_id: int, filter_cols: tuple) -> tuple | None:
        """Linear scan of the segment holding the table."""
        reader = self._owner.get(table_id)
        return reader.scan_matching(table_id, filter_cols) if reader is not None else None

    def __len__(self) -> int:
        return len(self.segments)

    def __bool__(self) -> bool:
        return bool(self.segments)


SHARED_SYMBOL_MAGIC = b"SYMB"
SHARED_SYMBOL_VERSION = 1

# Separator joining a repository-relative file path and a symbol name into one file-scoped key
FILE_SCOPE_SEP = "\x00"
SYMBOL_SLOT_SIZE = 24
EMPTY_SYMBOL_SLOT = b"\x00" * 16 + b"\xff" * 8


def file_scoped_key(rel_file: str, sym_name: str) -> str:
    """Return the shared-table key binding `sym_name` to the file that defines it."""
    return f"{rel_file}{FILE_SCOPE_SEP}{sym_name}"


class SharedSymbolMappingView:
    """Mapping-like view over a SharedSymbolReader for (name, type_id) -> ast_id lookups."""

    def __init__(self, reader: SharedSymbolReader) -> None:
        self._reader = reader

    def __getitem__(self, key: tuple[str, int]) -> int:
        val = self._reader.get_symbol(key[0], key[1])
        if val is None:
            raise KeyError(key)
        return val

    def get(self, key: tuple[str, int], default: Any = None) -> Any:
        val = self._reader.get_symbol(key[0], key[1])
        return val if val is not None else default

    def __contains__(self, key: Any) -> bool:
        if isinstance(key, tuple) and len(key) >= 2:
            return self._reader.get_symbol(key[0], key[1]) is not None
        return False

    def __len__(self) -> int:
        return self._reader.sym_count

    def __bool__(self) -> bool:
        return self._reader.sym_count > 0


class SharedNameMappingView:
    """Mapping-like view over a SharedSymbolReader for name -> ast_id lookups."""

    def __init__(self, reader: SharedSymbolReader) -> None:
        self._reader = reader

    def __getitem__(self, key: str) -> int:
        val = self._reader.get_name(key)
        if val is None:
            raise KeyError(key)
        return val

    def get(self, key: str, default: Any = None) -> Any:
        val = self._reader.get_name(key)
        return val if val is not None else default

    def __contains__(self, key: Any) -> bool:
        if isinstance(key, str):
            return self._reader.get_name(key) is not None
        return False

    def __len__(self) -> int:
        return self._reader.name_count

    def __bool__(self) -> bool:
        return self._reader.name_count > 0


class SharedSymbolReader:
    """Zero-deserialization lock-free reader querying symbols directly from shared memory."""

    def __init__(self, mm: mmap.mmap) -> None:
        self.mm = mm
        self.mv = memoryview(mm)
        (
            magic,
            version,
            self.sym_count,
            self.name_count,
            self.sym_idx_offset,
            self.sym_cap,
            self.name_idx_offset,
            self.name_cap,
            self.str_offset,
            self.str_size,
        ) = struct.unpack_from("<4sIIQQQQQQQ", self.mv, 0)
        self.sym_mask = self.sym_cap - 1
        self.name_mask = self.name_cap - 1
        self.symbols = SharedSymbolMappingView(self)
        self.names = SharedNameMappingView(self)

    def get_symbol(self, sym_name: str, sym_type: int) -> int | None:
        """Query ast_id for (sym_name, sym_type) in O(1) time without heap allocations."""
        if not sym_name:
            return None
        h64 = (_hash64(sym_name) ^ (sym_type * 0x9E3779B97F4A7C15)) & 0xFFFFFFFFFFFFFFFF
        if h64 == 0:
            h64 = 1
        step = 1
        slot = h64 & self.sym_mask
        name_bytes = sym_name.encode("utf-8")
        target_len = len(name_bytes)
        SLOT_SIZE = 24
        EMPTY_AST_ID = 0xFFFFFFFFFFFFFFFF

        while True:
            slot_off = self.sym_idx_offset + slot * SLOT_SIZE
            cur_h64, s_off, s_len, cur_type, _ = struct.unpack_from("<QIHHI", self.mv, slot_off)
            cur_ast_id = struct.unpack_from("<Q", self.mv, slot_off + 16)[0]
            if cur_ast_id == EMPTY_AST_ID:
                return None
            if cur_h64 == h64 and cur_type == sym_type and s_len == target_len:
                if self.mv[self.str_offset + s_off : self.str_offset + s_off + s_len] == name_bytes:
                    return cur_ast_id
            slot = (slot + step) & self.sym_mask
            step += 1
            if step > self.sym_cap:
                return None

    def get_name(self, sym_name: str) -> int | None:
        """Query ast_id for sym_name in O(1) time."""
        if not sym_name:
            return None
        h64 = _hash64(sym_name)
        if h64 == 0:
            h64 = 1
        step = 1
        slot = h64 & self.name_mask
        name_bytes = sym_name.encode("utf-8")
        target_len = len(name_bytes)
        SLOT_SIZE = 24
        EMPTY_AST_ID = 0xFFFFFFFFFFFFFFFF

        while True:
            slot_off = self.name_idx_offset + slot * SLOT_SIZE
            cur_h64, s_off, s_len, _, _ = struct.unpack_from("<QIHHI", self.mv, slot_off)
            cur_ast_id = struct.unpack_from("<Q", self.mv, slot_off + 16)[0]
            if cur_ast_id == EMPTY_AST_ID:
                return None
            if cur_h64 == h64 and s_len == target_len:
                if self.mv[self.str_offset + s_off : self.str_offset + s_off + s_len] == name_bytes:
                    return cur_ast_id
            slot = (slot + step) & self.name_mask
            step += 1
            if step > self.name_cap:
                return None

    def get_file_symbol(self, rel_file: str, sym_name: str, sym_type: int) -> int | None:
        """Query ast_id for `(sym_name, sym_type)` defined in `rel_file` (tables built by `build_file_scoped`)."""
        if not rel_file or not sym_name:
            return None
        return self.get_symbol(file_scoped_key(rel_file, sym_name), sym_type)

    def get_file_name(self, rel_file: str, sym_name: str) -> int | None:
        """Query ast_id for `sym_name` of any type defined in `rel_file` (tables built by `build_file_scoped`)."""
        if not rel_file or not sym_name:
            return None
        return self.get_name(file_scoped_key(rel_file, sym_name))

    @property
    def nbytes(self) -> int:
        """Size of the shared memory mapping in bytes (0 once closed)."""
        return len(self.mv) if self.mv is not None else 0

    def __contains__(self, key: Any) -> bool:
        if isinstance(key, tuple) and len(key) >= 2:
            return self.get_symbol(key[0], key[1]) is not None
        if isinstance(key, str):
            return self.get_name(key) is not None
        return False

    def __getitem__(self, key: Any) -> int:
        if isinstance(key, tuple) and len(key) >= 2:
            val = self.get_symbol(key[0], key[1])
        elif isinstance(key, str):
            val = self.get_name(key)
        else:
            raise KeyError(key)
        if val is None:
            raise KeyError(key)
        return val

    def get(self, key: Any, default: Any = None) -> Any:
        if isinstance(key, tuple) and len(key) >= 2:
            val = self.get_symbol(key[0], key[1])
        elif isinstance(key, str):
            val = self.get_name(key)
        else:
            return default
        return val if val is not None else default

    def __len__(self) -> int:
        return self.sym_count

    def __bool__(self) -> bool:
        return self.sym_count > 0

    def items(self):
        """Yield ((sym_name, sym_type), ast_id) for all packed symbols directly from shared memory."""
        SLOT_SIZE = 24
        EMPTY_AST_ID = 0xFFFFFFFFFFFFFFFF
        for slot in range(self.sym_cap):
            slot_off = self.sym_idx_offset + slot * SLOT_SIZE
            cur_h64, s_off, s_len, cur_type, _ = struct.unpack_from("<QIHHI", self.mv, slot_off)
            cur_ast_id = struct.unpack_from("<Q", self.mv, slot_off + 16)[0]
            if cur_ast_id != EMPTY_AST_ID:
                sym_name = bytes(self.mv[self.str_offset + s_off : self.str_offset + s_off + s_len]).decode("utf-8")
                yield (sym_name, cur_type), cur_ast_id

    def close(self) -> None:
        """Release memoryview and close underlying shared memory mapping."""
        try:
            if hasattr(self, "mv") and self.mv is not None:
                self.mv.release()
                self.mv = None
        except Exception:
            pass
        try:
            if hasattr(self, "mm") and self.mm is not None:
                self.mm.close()
                self.mm = None
        except Exception:
            pass


class SharedSymbolBuilder:
    """Builds a compact open-addressing shared memory hash table for header symbols."""

    def __init__(self, mode: str = "auto") -> None:
        self.mode = "off" if mode == "4k" else mode

    def build_file_scoped(
        self,
        file_symbols: dict[str, dict[tuple[str, int], int]],
        extra_symbols: dict[tuple[str, int], int] | None = None,
        extra_names: dict[str, int] | None = None,
    ) -> SharedSymbolReader | None:
        """Build a table keyed by defining file, queried with `get_file_symbol()` / `get_file_name()`.
        Also indexes unscoped (sym_name, sym_type) and sym_name for O(1) in-memory fallback.

        Args:
            file_symbols: Per-file symbol maps `rel_file -> {(sym_name, sym_type): ast_id}`.
            extra_symbols: Optional extra (sym_name, sym_type) -> ast_id mappings to index unscoped.
            extra_names: Optional extra sym_name -> ast_id mappings to index unscoped.

        Returns:
            Reader over the packed table, or None when no file publishes a symbol.
        """
        symbols_map: dict[tuple[str, int], int] = {}
        names_map: dict[str, int] = {}
        for rel_file, f_syms in file_symbols.items():
            if not rel_file or not f_syms:
                continue
            for (sym_name, sym_type), ast_id in f_syms.items():
                if not sym_name or ast_id is None:
                    continue
                key = file_scoped_key(rel_file, sym_name)
                symbols_map[(key, sym_type)] = ast_id
                names_map[key] = ast_id

        if extra_symbols:
            for (sym_name, sym_type), ast_id in extra_symbols.items():
                if sym_name and ast_id is not None and (sym_name, sym_type) not in symbols_map:
                    symbols_map[(sym_name, sym_type)] = ast_id
        if extra_names:
            for sym_name, ast_id in extra_names.items():
                if sym_name and ast_id is not None and sym_name not in names_map:
                    names_map[sym_name] = ast_id

        return self.build(symbols_map, names_map)

    def build(
        self,
        symbols_map: dict[tuple[str, int], int],
        names_map: dict[str, int] | None = None,
    ) -> SharedSymbolReader | None:
        if not symbols_map and not names_map:
            return None

        if names_map is None:
            names_map = {k[0]: v for k, v in symbols_map.items()}

        unique_names = set(names_map.keys()) | {k[0] for k in symbols_map.keys()}
        str_pool_bytes = bytearray()
        str_offsets: dict[str, tuple[int, int]] = {}
        for name in unique_names:
            encoded = name.encode("utf-8")
            str_offsets[name] = (len(str_pool_bytes), len(encoded))
            str_pool_bytes.extend(encoded)

        sym_cap = 16
        while sym_cap < max(16, len(symbols_map) * 2):
            sym_cap *= 2

        name_cap = 16
        while name_cap < max(16, len(names_map) * 2):
            name_cap *= 2

        HEADER_SIZE = 64
        SLOT_SIZE = 24
        sym_idx_size = sym_cap * SLOT_SIZE
        name_idx_size = name_cap * SLOT_SIZE
        str_size = len(str_pool_bytes)

        total_size = HEADER_SIZE + sym_idx_size + name_idx_size + str_size + 1024
        # Symbol tables are tens to hundreds of MB: a 1GB HugeTLB page would waste most of a page per table
        mm, mode_used, page_sz = SharedBufferAllocator.allocate(total_size, mode=self.mode, allow_1g=False)
        mv = memoryview(mm)

        sym_idx_offset = HEADER_SIZE
        name_idx_offset = sym_idx_offset + sym_idx_size
        str_offset = name_idx_offset + name_idx_size

        struct.pack_into(
            "<4sIIQQQQQQQ",
            mv,
            0,
            SHARED_SYMBOL_MAGIC,
            SHARED_SYMBOL_VERSION,
            len(symbols_map),
            len(names_map),
            sym_idx_offset,
            sym_cap,
            name_idx_offset,
            name_cap,
            str_offset,
            str_size,
        )

        EMPTY_AST_ID = 0xFFFFFFFFFFFFFFFF
        # Initialize every empty slot with one bulk copy instead of two struct calls per slot
        mv[sym_idx_offset : sym_idx_offset + sym_idx_size] = EMPTY_SYMBOL_SLOT * sym_cap
        mv[name_idx_offset : name_idx_offset + name_idx_size] = EMPTY_SYMBOL_SLOT * name_cap

        mv[str_offset : str_offset + str_size] = str_pool_bytes

        sym_mask = sym_cap - 1
        for (sym_name, sym_type), ast_id in symbols_map.items():
            s_off, s_len = str_offsets[sym_name]
            h64 = (_hash64(sym_name) ^ (sym_type * 0x9E3779B97F4A7C15)) & 0xFFFFFFFFFFFFFFFF
            if h64 == 0:
                h64 = 1
            step = 1
            slot = h64 & sym_mask
            while True:
                slot_off = sym_idx_offset + slot * SLOT_SIZE
                cur_ast_id = struct.unpack_from("<Q", mv, slot_off + 16)[0]
                if cur_ast_id == EMPTY_AST_ID:
                    struct.pack_into("<QIHHI", mv, slot_off, h64, s_off, s_len, sym_type, 0)
                    struct.pack_into("<Q", mv, slot_off + 16, ast_id)
                    break
                slot = (slot + step) & sym_mask
                step += 1

        name_mask = name_cap - 1
        for sym_name, ast_id in names_map.items():
            s_off, s_len = str_offsets[sym_name]
            h64 = _hash64(sym_name)
            if h64 == 0:
                h64 = 1
            step = 1
            slot = h64 & name_mask
            while True:
                slot_off = name_idx_offset + slot * SLOT_SIZE
                cur_ast_id = struct.unpack_from("<Q", mv, slot_off + 16)[0]
                if cur_ast_id == EMPTY_AST_ID:
                    struct.pack_into("<QIHHI", mv, slot_off, h64, s_off, s_len, 0, 0)
                    struct.pack_into("<Q", mv, slot_off + 16, ast_id)
                    break
                slot = (slot + step) & name_mask
                step += 1

        return SharedSymbolReader(mm)


class SharedSymbolChain:
    """Ordered set of file-scoped symbol segments queried newest-first.

    Stage 2 appends one segment per epoch holding only the symbols published since the previous
    segment, instead of re-packing every symbol at each epoch boundary. A newer segment shadows an
    older one for the same file.
    """

    def __init__(self) -> None:
        self.segments: list[SharedSymbolReader] = []

    def append(self, segment: SharedSymbolReader) -> None:
        self.segments.append(segment)

    def get_file_symbol(self, rel_file: str, sym_name: str, sym_type: int) -> int | None:
        for segment in reversed(self.segments):
            val = segment.get_file_symbol(rel_file, sym_name, sym_type)
            if val is not None:
                return val
        return None

    def get_file_name(self, rel_file: str, sym_name: str) -> int | None:
        for segment in reversed(self.segments):
            val = segment.get_file_name(rel_file, sym_name)
            if val is not None:
                return val
        return None

    def get_symbol(self, sym_name: str, sym_type: int) -> int | None:
        for segment in reversed(self.segments):
            val = segment.get_symbol(sym_name, sym_type)
            if val is not None:
                return val
        return None

    def get_name(self, sym_name: str) -> int | None:
        for segment in reversed(self.segments):
            val = segment.get_name(sym_name)
            if val is not None:
                return val
        return None

    @property
    def sym_count(self) -> int:
        return sum(segment.sym_count for segment in self.segments)

    @property
    def nbytes(self) -> int:
        return sum(segment.nbytes for segment in self.segments)

    def __len__(self) -> int:
        return self.sym_count

    def __bool__(self) -> bool:
        return any(segment.sym_count > 0 for segment in self.segments)

    def close(self) -> None:
        """Unmap every segment."""
        for segment in self.segments:
            segment.close()
        self.segments = []


_MISSING = object()


class SharedTableRowSequenceView:
    """Zero-copy sequence view over a table in SharedTableReader with dynamic mutation support.
    Evicts preloaded row heap collections while transparently fulfilling list-like operations."""

    def __init__(self, reader: SharedTableReader, table_id: int) -> None:
        self._reader = reader
        self._table_id = table_id
        self._meta = reader.tables.get(table_id, {})
        self._shared_count = self._meta.get("row_count", 0)
        self._rec_size = self._meta.get("col_count", 0) * 9
        self._data_offset = self._meta.get("data_offset", 0)
        self._appended: list[tuple] = []
        self._overwritten: dict[int, tuple] = {}

    def __len__(self) -> int:
        return self._shared_count + len(self._appended)

    def __bool__(self) -> bool:
        return len(self) > 0

    def __getitem__(self, idx: int | slice) -> Any:
        if isinstance(idx, slice):
            return [self[i] for i in range(*idx.indices(len(self)))]
        total = len(self)
        if idx < 0:
            idx += total
        if idx < 0 or idx >= total:
            raise IndexError("SharedTableRowSequenceView index out of range")
        if idx in self._overwritten:
            return self._overwritten[idx]
        if idx < self._shared_count:
            rec_off = self._data_offset + idx * self._rec_size
            return self._reader._unpack_row(self._meta, rec_off)
        return self._appended[idx - self._shared_count]

    def __setitem__(self, idx: int, value: tuple) -> None:
        total = len(self)
        if idx < 0:
            idx += total
        if idx < 0 or idx >= total:
            raise IndexError("SharedTableRowSequenceView assignment index out of range")
        if idx < self._shared_count:
            self._overwritten[idx] = value
        else:
            self._appended[idx - self._shared_count] = value

    def append(self, row: tuple) -> None:
        self._appended.append(row)

    def extend(self, rows: Sequence[tuple]) -> None:
        self._appended.extend(rows)

    def remove(self, row: tuple) -> None:
        if row in self._appended:
            self._appended.remove(row)
            return
        for i in range(self._shared_count):
            if self[i] == row:
                self._overwritten[i] = None
                return
        raise ValueError("row not found in SharedTableRowSequenceView")

    def __iter__(self):
        for i in range(self._shared_count):
            if i in self._overwritten:
                val = self._overwritten[i]
                if val is not None:
                    yield val
            else:
                rec_off = self._data_offset + i * self._rec_size
                yield self._reader._unpack_row(self._meta, rec_off)
        for row in self._appended:
            yield row

    def clear(self) -> None:
        self._shared_count = 0
        self._appended.clear()
        self._overwritten.clear()

    def drop_appended(self) -> None:
        """Forget rows appended on top of the shared snapshot (the snapshot rows stay readable)."""
        self._appended.clear()


class SharedTablePKView:
    """Zero-copy mapping view over primary keys in SharedTableReader with dynamic mutation support."""

    def __init__(self, reader: SharedTableReader, table_id: int, local: dict[Any, tuple] | None = None) -> None:
        self._reader = reader
        self._table_id = table_id
        self._meta = reader.tables.get(table_id, {})
        # Rows added on top of the snapshot (may be a dict shared with the owner, e.g. a carried overlay)
        self._local: dict[Any, tuple] = local if local is not None else {}
        self._deleted: set[Any] = set()

    def drain_local(self) -> list[Any]:
        """Remove and return the keys of rows added on top of the shared snapshot."""
        keys = list(self._local)
        self._local.clear()
        return keys

    def _sanitize(self, key: Any) -> Any:
        if isinstance(key, (bytearray, memoryview)):
            return bytes(key)
        if isinstance(key, tuple):
            return tuple(bytes(x) if isinstance(x, (bytearray, memoryview)) else x for x in key)
        return key

    def __contains__(self, key: Any) -> bool:
        k = self._sanitize(key)
        if k in self._deleted:
            return False
        if k in self._local:
            return True
        return self._reader.get_row_by_pk(self._table_id, k) is not None

    def get(self, key: Any, default: Any = None) -> Any:
        k = self._sanitize(key)
        if k in self._deleted:
            return default
        if k in self._local:
            return self._local[k]
        row = self._reader.get_row_by_pk(self._table_id, k)
        return row if row is not None else default

    def __getitem__(self, key: Any) -> Any:
        val = self.get(key, _MISSING)
        if val is _MISSING:
            raise KeyError(key)
        return val

    def __setitem__(self, key: Any, value: tuple) -> None:
        k = self._sanitize(key)
        self._deleted.discard(k)
        self._local[k] = value

    def pop(self, key: Any, default: Any = _MISSING) -> Any:
        k = self._sanitize(key)
        if k in self._local:
            return self._local.pop(k)
        row = self._reader.get_row_by_pk(self._table_id, k)
        if row is not None and k not in self._deleted:
            self._deleted.add(k)
            return row
        if default is not _MISSING:
            return default
        raise KeyError(key)

    def keys(self) -> set[Any]:
        res = set(self._local.keys())
        if self._reader.contains_table(self._table_id):
            t_meta = self._meta
            pk_cols = t_meta.get("primary", ())
            if pk_cols:
                rec_size = t_meta.get("col_count", 0) * 9
                data_offset = t_meta.get("data_offset", 0)
                for i in range(t_meta.get("row_count", 0)):
                    rec_off = data_offset + i * rec_size
                    row = self._reader._unpack_row(t_meta, rec_off)
                    if len(pk_cols) == 1:
                        pk = row[pk_cols[0]]
                    else:
                        pk = tuple(row[c] for c in pk_cols)
                    pk = self._sanitize(pk)
                    if pk not in self._deleted:
                        res.add(pk)
        return res

    def __len__(self) -> int:
        base_cnt = self._meta.get("row_count", 0) if self._reader.contains_table(self._table_id) else 0
        return max(0, base_cnt + len(self._local) - len(self._deleted))

    def __bool__(self) -> bool:
        return len(self) > 0

    def __iter__(self):
        return iter(self.keys())

    def items(self):
        for k in self.keys():
            yield k, self[k]

    def values(self):
        for k in self.keys():
            yield self[k]

    def clear(self) -> None:
        self._local.clear()
        if self._reader.contains_table(self._table_id):
            t_meta = self._meta
            pk_cols = t_meta.get("primary", ())
            if pk_cols:
                rec_size = t_meta.get("col_count", 0) * 9
                data_offset = t_meta.get("data_offset", 0)
                for i in range(t_meta.get("row_count", 0)):
                    rec_off = data_offset + i * rec_size
                    row = self._reader._unpack_row(t_meta, rec_off)
                    if len(pk_cols) == 1:
                        pk = row[pk_cols[0]]
                    else:
                        pk = tuple(row[c] for c in pk_cols)
                    self._deleted.add(self._sanitize(pk))


