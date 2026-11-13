"""webapp/backend/services/pahole_service.py - Struct Memory Layout & 64-Byte Cacheline Visualizer."""
from __future__ import annotations
import re
from typing import Any
from fastapi import HTTPException
from db_engine.spec import Agg, Join, Value
from webapp.backend.database.pool import get_reader
from webapp.backend.database.helpers import safe_decode
from webapp.backend.services.git_reader import git_reader
from webapp.backend.services.symbol_service import SymbolService
from webapp.backend.services.tree_resolver import best_path, definition_arch, definition_rank

# m_type_descriptor names of struct and union definitions (a variable or macro of the same name is no layout)
STRUCT_DECL_TYPES = ("C_structdecl", "C_uniondecl")
# Trees that carry their own copy of kernel structs for host programs (tools/firewire/list.h, ...)
HOST_TREES = ("tools/", "scripts/", "samples/", "Documentation/")

# A member's type fallback sizes when compiler AST attributes (size_bytes) are absent.
_EXACT_SIZES: dict[str, tuple[int, int]] = {
    "char": (1, 1), "bool": (1, 1), "short": (2, 2), "int": (4, 4), "float": (4, 4), "enum": (4, 4),
    "long": (8, 8), "double": (8, 8), "pointer": (8, 8),
    "void": (8, 8),           # a void member is a void pointer
    "functionproto": (8, 8),  # a function pointer
}
_GUESSED_SIZES: dict[str, tuple[int, int]] = {"unsigned": (4, 4), "signed": (4, 4)}
_DISPLAY_TYPES = {
    "void": "void *", "SCtypedef": "typedef", "structdecl": "struct", "struct": "struct",
    "Compound": "struct", "uniondecl": "union", "functionproto": "function pointer",
}


def _category(raw_type: str | None) -> str:
    t = (raw_type or "").strip()
    return t[2:] if t.startswith("C_") else t


def _member_size(category: str) -> tuple[int, int, bool]:
    """(size, alignment, estimated) of a member type category."""
    if category in _EXACT_SIZES:
        return (*_EXACT_SIZES[category], False)
    return (*_GUESSED_SIZES.get(category, (8, 8)), True)


class PaholeService:
    """Member offsets, alignment holes and 64-byte cachelines of a C struct, verified via compiler AST attributes."""

    @staticmethod
    def _extract_source_field_types(version_name: str, file_path: str | None, line_s: int, line_e: int) -> dict[str, str]:
        """Extract clean C declarator type strings from the struct definition in Git source."""
        field_types: dict[str, str] = {}
        if not file_path:
            return field_types
        try:
            blob_bytes, is_binary, _ = git_reader.read_file(version_name, file_path)
            if not blob_bytes or is_binary:
                return field_types
            text = blob_bytes.decode("utf-8", errors="replace")
            lines = text.splitlines()[max(0, line_s - 1):line_e]
            for line in lines[1:-1]:
                line = re.sub(r"/\*.*?\*/", "", line).strip()
                if not line or line.startswith("//") or line.startswith("#"):
                    continue
                # Function pointer: void (*done)(struct foo *);
                fn_m = re.match(r"^(.*?)\(\s*\*\s*([a-zA-Z_]\w*)\s*\)\s*(\(.*?\))\s*;", line)
                if fn_m:
                    field_types[fn_m.group(2)] = f"{fn_m.group(1).strip()}(*){fn_m.group(3).strip()}"
                    continue
                # Bitfield: int flag: 1;
                bf_m = re.match(r"^(.*?)\s+([a-zA-Z_]\w*)\s*:\s*(\d+)\s*;", line)
                if bf_m:
                    field_types[bf_m.group(2)] = f"{bf_m.group(1).strip()} : {bf_m.group(3)}"
                    continue
                if not line.endswith(";"):
                    continue
                line_body = line[:-1].strip()
                m = re.match(r"^(.*?)\s+((?:(?:\*+\s*)?[a-zA-Z_]\w*(?:\s*\[[^\]]*\])?\s*,\s*)*(?:(?:\*+\s*)?[a-zA-Z_]\w*(?:\s*\[[^\]]*\])?))$", line_body)
                if not m:
                    continue
                base_type = m.group(1).strip()
                decls_str = m.group(2).strip()
                for d in decls_str.split(","):
                    d = d.strip()
                    dm = re.match(r"^(\*+)?\s*([a-zA-Z_]\w*)\s*(\[[^\]]*\])?$", d)
                    if dm:
                        ptrs = (dm.group(1) or "").strip()
                        name = dm.group(2)
                        arr = (dm.group(3) or "").strip()
                        type_str = f"{base_type} {ptrs}".strip()
                        if arr:
                            type_str = f"{type_str} {arr}".strip()
                        field_types[name] = type_str
        except Exception:
            pass
        return field_types

    @staticmethod
    def _definition(reader: Any, version_name: str, name: str) -> tuple[str, dict[str, Any] | None]:
        """(revision, the struct/union definition of `name` in the revision's tree), preferring kernel
        headers, then the ranking of SymbolService (arch-aware), and definitions that have members."""
        _, rev, blob_paths = SymbolService._scope(reader, version_name)
        _, scope, bits = definition_arch(rev, None)
        rows = [
            r
            for r in reader.select_rows(
                ("m_symbol_def", "s"),
                {"ast_id": "s.ast_id", "blob_id": "s.blob_id", "line_s": "s.line_s", "line_e": "s.line_e", "linkage": "s.linkage"},
                {"s.name": name, "t.name": list(STRUCT_DECL_TYPES)},
                joins=[Join(("m_type_descriptor", "t"), {"t.type_id": "s.type_id"})],
                as_dict=True,
            )
            if r["blob_id"] in blob_paths
        ]
        if not rows:
            return rev, None
        ast_ids = sorted({r["ast_id"] for r in rows})
        members = {
            r["ast_id"]: int(r["n"])
            for r in reader.aggregate(
                "m_ast_container", {"n": Agg("COUNT", "*")}, {"ast_id": ast_ids}, group_by=["ast_id"], as_dict=True
            )
        }
        for r in rows:
            r["fname"] = best_path(blob_paths[r["blob_id"]], scope, bits)

        def key(r: dict[str, Any]) -> tuple:
            path = r["fname"] or ""
            return (
                members.get(r["ast_id"], 0) == 0,  # a forward declaration has no members
                path.startswith(HOST_TREES),
                not path.endswith(".h"),
                definition_rank(path, r.get("linkage"), r["line_s"], scope, bits),
            )

        return rev, min(rows, key=key)

    def get_struct_layout(self, version_name: str, struct_name: str) -> dict[str, Any]:
        """Memory layout of a C struct or union (404 when the revision defines none)."""
        clean_name = struct_name.strip()
        for prefix in ("struct ", "union "):
            if clean_name.startswith(prefix):
                clean_name = clean_name[len(prefix):].strip()
        if not clean_name:
            raise HTTPException(status_code=400, detail="A struct name must be specified.")

        with get_reader() as reader:
            vname, definition = self._definition(reader, version_name, clean_name)
            if definition is None:
                raise HTTPException(status_code=404, detail=f"No struct or union '{clean_name}' is defined at '{version_name}'.")
            fields_raw = reader.select_rows(
                ("m_ast_container", "c"),
                {
                    "priority": "c.priority",
                    "ref_ast_id": "c.ref_ast_id",
                    "field_name": "ref.name",
                    "type_name": "td.name",
                    "ref_type_name": "rtd.name",
                    "attr_size": "a_size.val",
                    "attr_offset": "a_offset.val",
                    "attr_bitfield": "a_bitfield.val",
                },
                {"c.ast_id": definition["ast_id"]},
                joins=[
                    Join(("m_ast", "ref"), {"ref.ast_id": "c.ref_ast_id"}, "left"),
                    Join(("m_type_descriptor", "td"), {"td.type_id": "c.type_id"}, "left"),
                    Join(("m_type_descriptor", "rtd"), {"rtd.type_id": "ref.type_id"}, "left"),
                    Join(("m_ast_attribute", "a_size"), {"a_size.ast_id": "c.ref_ast_id", "a_size.name": Value("size_bytes")}, "left"),
                    Join(("m_ast_attribute", "a_offset"), {"a_offset.ast_id": "c.ref_ast_id", "a_offset.name": Value("offset_bits")}, "left"),
                    Join(("m_ast_attribute", "a_bitfield"), {"a_bitfield.ast_id": "c.ref_ast_id", "a_bitfield.name": Value("bitfield_width")}, "left"),
                ],
                order_by=["c.priority"],
                as_dict=True,
            )

        source_field_types = self._extract_source_field_types(
            vname, definition.get("fname"), definition.get("line_s") or 1, definition.get("line_e") or 1
        )

        members = []
        curr_offset = 0
        max_align = 1
        total_padding = 0
        holes = 0
        for r in fields_raw:
            field_name = safe_decode(r["field_name"]) or f"field_{r['priority']}"
            category = _category(safe_decode(r["type_name"]) or safe_decode(r["ref_type_name"]) or "int")
            attr_size = r.get("attr_size")
            attr_offset = r.get("attr_offset")
            attr_bitfield = r.get("attr_bitfield")

            # Compiler-extracted size from Clang (size_bytes attribute)
            if attr_size is not None and int(attr_size) >= 0:
                sz = int(attr_size)
                estimated = False
            else:
                sz, _, estimated = _member_size(category)

            # Alignment derivation
            if category in _EXACT_SIZES:
                al = _EXACT_SIZES[category][1]
            elif category in _GUESSED_SIZES:
                al = _GUESSED_SIZES[category][1]
            else:
                al = min(8, max(1, sz if sz <= 8 and (sz & (sz - 1)) == 0 else 8))
            if sz > 0 and sz < al:
                al = sz
            max_align = max(max_align, al)

            # Offset & padding hole derivation (offset_bits attribute)
            bit_offset = None
            if attr_offset is not None and int(attr_offset) >= 0:
                exact_offset_bits = int(attr_offset)
                exact_byte_offset = exact_offset_bits // 8
                rem_bits = exact_offset_bits % 8
                if rem_bits > 0:
                    bit_offset = rem_bits
                if exact_byte_offset > curr_offset:
                    pad_before = exact_byte_offset - curr_offset
                    holes += 1
                    total_padding += pad_before
                    curr_offset = exact_byte_offset
                else:
                    pad_before = 0
                    curr_offset = exact_byte_offset
            else:
                pad_before = (al - (curr_offset % al)) % al
                if pad_before:
                    holes += 1
                    total_padding += pad_before
                    curr_offset += pad_before

            disp_type = source_field_types.get(field_name, _DISPLAY_TYPES.get(category, category))
            members.append({
                "name": field_name,
                "type": disp_type,
                "size": sz,
                "alignment": al,
                "estimated": estimated,
                "offset": curr_offset,
                "padding_before": pad_before,
                "cacheline_idx": curr_offset // 64,
                "cacheline_offset": curr_offset % 64,
                "crosses_cacheline": (curr_offset // 64) != ((curr_offset + sz - 1) // 64) if sz > 0 else False,
                "bitfield_width": int(attr_bitfield) if attr_bitfield else None,
                "bit_offset": bit_offset,
            })

            if attr_bitfield and int(attr_bitfield) > 0 and attr_offset is not None:
                next_bit = int(attr_offset) + int(attr_bitfield)
                curr_offset = (next_bit + 7) // 8
            else:
                curr_offset += sz

        total_size = reordered_total = 0
        cache_lines_used = 0
        suggested_order: list[str] = []
        if members:
            tail_padding = (max_align - (curr_offset % max_align)) % max_align
            if tail_padding:
                holes += 1
            total_padding += tail_padding
            total_size = curr_offset + tail_padding
            cache_lines_used = (total_size - 1) // 64 + 1
            reordered = sorted(members, key=lambda m: (-m["alignment"], -m["size"]))
            pos = 0
            for m in reordered:
                pos += (m["alignment"] - (pos % m["alignment"])) % m["alignment"] + m["size"]
            reordered_total = pos + (max_align - (pos % max_align)) % max_align
            suggested_order = [m["name"] for m in reordered]

        estimated_members = sum(1 for m in members if m["estimated"])
        return {
            "version": vname,
            "struct_name": f"struct {clean_name}",
            "file_path": definition["fname"],
            "line_s": definition["line_s"] or 1,
            "line_e": definition["line_e"] or 1,
            # Exact whenever all members have compiler attributes (is_estimate = False)
            "is_estimate": estimated_members > 0,
            "estimated_members": estimated_members,
            "total_size": total_size,
            "alignment": max_align,
            "padding_bytes": total_padding,
            "holes_count": holes,
            "cache_lines_used": cache_lines_used,
            "members": members,
            "optimization": {
                "current_size": total_size,
                "optimized_size": reordered_total,
                "bytes_saved": max(0, total_size - reordered_total),
                "suggested_order": suggested_order,
            },
        }
