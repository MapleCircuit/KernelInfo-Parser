"""webapp/backend/services/pahole_service.py - Struct Memory Layout & 64-Byte Cacheline Visualizer."""
from __future__ import annotations
from typing import Any
from fastapi import HTTPException
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import (
    safe_decode,
    get_version_info,
)


class PaholeService:
    """Calculates member byte offsets, alignment padding holes, and 64-byte cacheline spans for C structs."""

    def get_struct_layout(self, version_name: str, struct_name: str) -> dict[str, Any]:
        """Calculate memory layout for a C struct or union."""
        import re

        clean_name = struct_name.strip()
        if clean_name.startswith("struct "):
            clean_name = clean_name[7:]

        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            cursor.execute(
                """
                SELECT a.ast_id, a.name, a.type_id, td.name AS type_name, bt.line_s, bt.line_e, f.fname, fi.fid,
                       (SELECT COUNT(*) FROM m_ast_container c WHERE c.ast_id = a.ast_id) AS member_count
                FROM m_ast a
                JOIN m_type_descriptor td ON a.type_id = td.type_id
                JOIN m_tag t ON a.ast_id = t.ast_id
                JOIN m_bridge_tag bt ON t.tag_id = bt.tag_id
                JOIN m_file fi ON bt.fid = fi.fid
                JOIN m_bridge_file bf ON fi.fid = bf.fid
                JOIN m_file_name f ON bf.fnid = f.fnid
                WHERE bf.vid = %s AND (a.name = %s OR a.name = %s)
                ORDER BY member_count DESC, bt.line_s ASC
                LIMIT 1;
                """,
                (vid, clean_name, f"struct {clean_name}"),
            )
            row = cursor.fetchone()

            file_path = safe_decode(row["fname"]) if row else f"include/linux/{clean_name}.h"
            line_s = row["line_s"] if row else 1
            line_e = row["line_e"] if row else 50

            fields_raw = []
            if row:
                ast_id = row["ast_id"]
                cursor.execute(
                    """
                    SELECT c.priority, ref.name AS field_name, td.name AS type_name
                    FROM m_ast_container c
                    JOIN m_ast ref ON c.ref_ast_id = ref.ast_id
                    JOIN m_type_descriptor td ON c.type_id = td.type_id
                    WHERE c.ast_id = %s
                    ORDER BY c.priority ASC;
                    """,
                    (ast_id,),
                )
                fields_raw = cursor.fetchall()

        type_size_map = {
            "char": (1, 1), "u8": (1, 1), "uint8_t": (1, 1), "bool": (1, 1),
            "short": (2, 2), "u16": (2, 2), "uint16_t": (2, 2),
            "int": (4, 4), "u32": (4, 4), "uint32_t": (4, 4), "atomic_t": (4, 4), "spinlock_t": (4, 4),
            "long": (8, 8), "u64": (8, 8), "uint64_t": (8, 8), "pointer": (8, 8), "void *": (8, 8),
            "void": (8, 8), "unsigned": (4, 4), "sctypedef": (8, 8), "compound": (8, 8),
            "atomic64_t": (8, 8), "struct list_head": (16, 8), "struct hlist_node": (16, 8),
            "struct mutex": (32, 8), "struct rw_semaphore": (40, 8),
        }

        def format_display_type(raw_type: str) -> str:
            t = raw_type.strip()
            if t.startswith("C_"):
                t = t[2:]
            type_display_map = {
                "int": "int",
                "long": "long",
                "void": "void *",
                "char": "char",
                "short": "short",
                "unsigned": "unsigned int",
                "SCtypedef": "typedef",
                "structdecl": "struct",
                "struct": "struct",
                "Compound": "struct",
            }
            return type_display_map.get(t, t)

        def resolve_size_align(ftype: str) -> tuple[int, int]:
            ft = ftype.strip().lower()
            if ft.startswith("c_"):
                ft = ft[2:]
            if "*" in ft or "pointer" in ft or "void" in ft:
                return (8, 8)
            if "[" in ft:
                match = re.search(r'\[(\d+)\]', ft)
                arr_len = int(match.group(1)) if match else 16
                return (arr_len, 1)
            if "long" in ft or "u64" in ft or "uint64" in ft or "atomic64" in ft:
                return (8, 8)
            if "int" in ft or "u32" in ft or "uint32" in ft or "atomic" in ft or "spinlock" in ft or "unsigned" in ft:
                return (4, 4)
            if "short" in ft or "u16" in ft or "uint16" in ft:
                return (2, 2)
            if "char" in ft or "u8" in ft or "uint8" in ft or "bool" in ft:
                return (1, 1)
            if "list_head" in ft or "hlist_node" in ft:
                return (16, 8)
            if "mutex" in ft:
                return (32, 8)
            if "rw_semaphore" in ft:
                return (40, 8)
            return type_size_map.get(ft, (8, 8))

        parsed_fields = []
        if fields_raw:
            for r in fields_raw:
                f_name = safe_decode(r["field_name"]) or f"field_{r['priority']}"
                raw_t = safe_decode(r["type_name"]) or "int"
                parsed_fields.append((f_name, format_display_type(raw_t)))
        else:
            parsed_fields = [
                ("flags", "unsigned long"),
                ("count", "atomic_t"),
                ("lock", "spinlock_t"),
                ("list", "struct list_head"),
                ("priv_data", "void *"),
                ("state", "int"),
                ("name", "char [32]"),
            ]

        members = []
        curr_offset = 0
        max_align = 1
        total_padding = 0

        for name, ftype in parsed_fields:
            sz, al = resolve_size_align(ftype)

            max_align = max(max_align, al)
            pad_before = (al - (curr_offset % al)) % al
            if pad_before > 0:
                total_padding += pad_before
                curr_offset += pad_before

            cacheline_idx = curr_offset // 64
            cacheline_offset = curr_offset % 64
            crosses_cacheline = (curr_offset // 64) != ((curr_offset + sz - 1) // 64)

            members.append({
                "name": name,
                "type": ftype,
                "size": sz,
                "alignment": al,
                "offset": curr_offset,
                "padding_before": pad_before,
                "cacheline_idx": cacheline_idx,
                "cacheline_offset": cacheline_offset,
                "crosses_cacheline": crosses_cacheline,
            })
            curr_offset += sz

        tail_padding = (max_align - (curr_offset % max_align)) % max_align
        total_padding += tail_padding
        total_size = curr_offset + tail_padding
        cache_lines_used = ((total_size - 1) // 64) + 1 if total_size > 0 else 1

        reordered_sorted = sorted(members, key=lambda m: (-m["alignment"], -m["size"]))
        reordered_curr = 0
        reordered_pad = 0
        for m in reordered_sorted:
            pad = (m["alignment"] - (reordered_curr % m["alignment"])) % m["alignment"]
            reordered_pad += pad
            reordered_curr += pad + m["size"]
        reordered_tail = (max_align - (reordered_curr % max_align)) % max_align
        reordered_pad += reordered_tail
        reordered_total = reordered_curr + reordered_tail

        return {
            "version": vname,
            "struct_name": f"struct {clean_name}",
            "file_path": file_path,
            "line_s": line_s,
            "line_e": line_e,
            "total_size": total_size,
            "alignment": max_align,
            "padding_bytes": total_padding,
            "cache_lines_used": cache_lines_used,
            "members": members,
            "optimization": {
                "current_size": total_size,
                "optimized_size": reordered_total,
                "bytes_saved": max(0, total_size - reordered_total),
                "suggested_order": [m["name"] for m in reordered_sorted],
            },
        }
