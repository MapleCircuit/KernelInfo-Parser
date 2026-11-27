"""webapp/backend/services/pahole_service.py - Struct Memory Layout & 64-Byte Cacheline Visualizer."""
from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import HTTPException

from db_engine.spec import Agg, Join, Value
from parser.struct_source import extract_struct_layout
from webapp.backend.database.helpers import safe_decode
from webapp.backend.database.pool import get_reader
from webapp.backend.services.git_reader import git_reader
from webapp.backend.services.symbol_service import SymbolService
from webapp.backend.services.tree_resolver import (
    best_path,
    definition_arch,
    definition_rank,
)

logger = logging.getLogger(__name__)

# m_type_descriptor names of struct and union definitions (a variable or macro of the same name is no layout)
STRUCT_DECL_TYPES = ("C_structdecl", "C_uniondecl")
# Trees that carry their own copy of kernel structs for host programs (tools/firewire/list.h, ...)
HOST_TREES = ("tools/", "scripts/", "samples/", "Documentation/")

# A member's type fallback sizes when compiler AST attributes (size_bytes) are absent.
_EXACT_SIZES: dict[str, tuple[int, int]] = {
    "char": (1, 1), "bool": (1, 1), "short": (2, 2), "int": (4, 4), "float": (4, 4), "enum": (4, 4),
    "long": (8, 8), "longlong": (8, 8), "double": (8, 8), "pointer": (8, 8),
    "void": (8, 8),           # a void member is a void pointer
    "functionproto": (8, 8),  # a function pointer
}
_GUESSED_SIZES: dict[str, tuple[int, int]] = {"unsigned": (4, 4), "signed": (4, 4)}
_DISPLAY_TYPES = {
    "void": "void *", "SCtypedef": "typedef", "structdecl": "struct", "struct": "struct",
    "Compound": "struct", "uniondecl": "union", "functionproto": "function pointer",
}
_LP64_PRIMITIVE_SIZES: dict[str, tuple[int, int]] = {
    "char": (1, 1), "signed char": (1, 1), "unsigned char": (1, 1),
    "bool": (1, 1), "_Bool": (1, 1),
    "u8": (1, 1), "__u8": (1, 1), "s8": (1, 1), "__s8": (1, 1), "uint8_t": (1, 1), "int8_t": (1, 1),
    "short": (2, 2), "short int": (2, 2), "signed short": (2, 2), "signed short int": (2, 2),
    "unsigned short": (2, 2), "unsigned short int": (2, 2),
    "u16": (2, 2), "__u16": (2, 2), "s16": (2, 2), "__s16": (2, 2), "uint16_t": (2, 2), "int16_t": (2, 2),
    "__le16": (2, 2), "__be16": (2, 2), "umode_t": (2, 2),
    "int": (4, 4), "signed": (4, 4), "signed int": (4, 4), "unsigned": (4, 4), "unsigned int": (4, 4),
    "float": (4, 4),
    "u32": (4, 4), "__u32": (4, 4), "s32": (4, 4), "__s32": (4, 4), "uint32_t": (4, 4), "int32_t": (4, 4),
    "__le32": (4, 4), "__be32": (4, 4), "atomic_t": (4, 4), "uid_t": (4, 4), "gid_t": (4, 4), "dev_t": (4, 4),
    "long": (8, 8), "long int": (8, 8), "signed long": (8, 8), "signed long int": (8, 8),
    "unsigned long": (8, 8), "unsigned long int": (8, 8),
    "long long": (8, 8), "long long int": (8, 8), "signed long long": (8, 8), "signed long long int": (8, 8),
    "unsigned long long": (8, 8), "unsigned long long int": (8, 8),
    "double": (8, 8),
    "u64": (8, 8), "__u64": (8, 8), "s64": (8, 8), "__s64": (8, 8), "uint64_t": (8, 8), "int64_t": (8, 8),
    "__le64": (8, 8), "__be64": (8, 8), "size_t": (8, 8), "ssize_t": (8, 8), "loff_t": (8, 8),
    "uintptr_t": (8, 8), "intptr_t": (8, 8), "atomic64_t": (8, 8), "atomic_long_t": (8, 8),
}
_QUAL_PREFIX_RE = re.compile(r"^(?:(?:const|volatile|restrict|__restrict|__restrict__|__iomem|__user|__rcu|__percpu)\s+)+")
_ARRAY_DIMS_RE = re.compile(r"(\s*\[[^\]]*\])+$")
_STRUCT_TAG_RE = re.compile(r"^(struct|union)\s+([a-zA-Z_]\w*)$")


def _parse_decl_type_info(type_str: str) -> tuple[str, int, tuple[int, int] | None, tuple[str, str] | None]:
    """Parse a clean C field type string into `(base_type, array_mult, (prim_size, prim_align) | None, (tag_kind, tag_name) | None)`."""
    if not type_str:
        return "", 1, None, None
    clean = type_str.split(":", 1)[0].strip()
    mult = 1
    m_arr = _ARRAY_DIMS_RE.search(clean)
    if m_arr:
        dims_str = m_arr.group(0)
        clean = clean[:m_arr.start()].strip()
        for dim_m in re.finditer(r"\[\s*(\d+)\s*\]", dims_str):
            d_val = int(dim_m.group(1))
            if d_val > 0:
                mult *= d_val
    clean = _QUAL_PREFIX_RE.sub("", clean).strip()
    if "(*)" in clean or clean.endswith("*"):
        return clean, mult, (8, 8), None
    clean_norm = " ".join(clean.split())
    if clean_norm in _LP64_PRIMITIVE_SIZES:
        return clean_norm, mult, _LP64_PRIMITIVE_SIZES[clean_norm], None
    m_tag = _STRUCT_TAG_RE.match(clean_norm)
    if m_tag:
        return clean_norm, mult, None, (m_tag.group(1), m_tag.group(2))
    return clean_norm, mult, None, None


def _category(raw_type: str | None) -> str:
    t = (raw_type or "").strip()
    return t.removeprefix("C_")


def _opt_int(val: Any) -> int | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _member_size(category: str) -> tuple[int, int, bool]:
    """(size, alignment, estimated) of a member type category."""
    if category in _EXACT_SIZES:
        return (*_EXACT_SIZES[category], False)
    return (*_GUESSED_SIZES.get(category, (8, 8)), True)


class PaholeService:
    """Member offsets, alignment holes and 64-byte cachelines of a C struct, verified via compiler AST attributes."""

    @staticmethod
    def _extract_source_field_info(
        version_name: str, file_path: str | None, line_s: int, line_e: int
    ) -> tuple[dict[str, str], set[str]]:
        """Extract clean C declarator type strings and declared field names from the struct definition in Git source."""
        field_types: dict[str, str] = {}
        valid_names: set[str] = set()
        if not file_path:
            return field_types, valid_names
        try:
            blob_bytes, is_binary, _ = git_reader.read_file(version_name, file_path)
            if not blob_bytes or is_binary:
                return field_types, valid_names
            text = blob_bytes.decode("utf-8", errors="replace")
            try:
                parsed_layout = extract_struct_layout(text, max(1, line_s), max(1, line_e))
                def _walk_items(items: list[dict[str, Any]]) -> None:
                    for it in items:
                        kind = it.get("kind")
                        if kind == "field":
                            fname = (it.get("name") or "").strip()
                            if fname:
                                valid_names.add(fname)
                                t_str = (it.get("type") or "").strip()
                                arr = (it.get("array") or "").strip()
                                bits = it.get("bits")
                                if arr:
                                    t_str = f"{t_str} {arr}".strip()
                                if bits is not None:
                                    t_str = f"{t_str} : {bits}".strip()
                                if t_str:
                                    field_types.setdefault(fname, t_str)
                        elif kind == "aggregate":
                            aname = (it.get("name") or "").strip()
                            if aname:
                                valid_names.add(aname)
                                if it.get("type_name"):
                                    field_types.setdefault(aname, str(it["type_name"]).strip())
                            _walk_items(it.get("children") or [])
                _walk_items(parsed_layout.get("items") or [])
            except Exception as exc:  # noqa: BLE001
                logger.debug("extract_struct_layout failed for %s:%s: %s", version_name, file_path, exc)

            lines = text.splitlines()[max(0, line_s - 1):line_e]
            for line in lines[1:-1]:
                line = re.sub(r"/\*.*?\*/", "", line).strip()
                if not line or line.startswith(("//", "#")):
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
        except Exception as exc:  # noqa: BLE001 - best-effort source field type extraction
            logger.debug("source field type extraction failed for %s:%s: %s", version_name, file_path, exc)
        return field_types, valid_names

    @classmethod
    def _extract_source_field_types(cls, version_name: str, file_path: str | None, line_s: int, line_e: int) -> dict[str, str]:
        """Extract clean C declarator type strings from the struct definition in Git source."""
        field_types, _ = cls._extract_source_field_info(version_name, file_path, line_s, line_e)
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

    @staticmethod
    def _fetch_container_fields(reader: Any, ast_id: int) -> list[dict[str, Any]]:
        return reader.select_rows(
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
                "attr_type_str": "a_tstr.val",
            },
            {"c.ast_id": ast_id},
            joins=[
                Join(("m_ast", "ref"), {"ref.ast_id": "c.ref_ast_id"}, "left"),
                Join(("m_type_descriptor", "td"), {"td.type_id": "c.type_id"}, "left"),
                Join(("m_type_descriptor", "rtd"), {"rtd.type_id": "ref.type_id"}, "left"),
                Join(("m_ast_attribute", "a_size"), {"a_size.ast_id": "c.ref_ast_id", "a_size.name": Value("size_bytes")}, "left"),
                Join(("m_ast_attribute", "a_offset"), {"a_offset.ast_id": "c.ref_ast_id", "a_offset.name": Value("offset_bits")}, "left"),
                Join(("m_ast_attribute", "a_bitfield"), {"a_bitfield.ast_id": "c.ref_ast_id", "a_bitfield.name": Value("bitfield_width")}, "left"),
                Join(("m_ast_attribute", "a_tstr"), {"a_tstr.ast_id": "c.ref_ast_id", "a_tstr.name": Value("type_str")}, "left"),
            ],
            order_by=["c.priority"],
            as_dict=True,
        )

    @staticmethod
    def _inspect_ast_type_tree(reader: Any, ref_ast_id: int | None) -> tuple[tuple[int, int] | None, tuple[str, str] | None]:
        """Inspect 1-2 levels of `m_ast_container` children under a member AST node when `size_bytes` and
        `type_str` are absent, returning `((prim_size, prim_align) | None, (tag_kind, tag_name) | None)`."""
        if not ref_ast_id:
            return None, None
        lvl1 = reader.select_rows(
            ("m_ast_container", "c"),
            {"ref_ast_id": "c.ref_ast_id", "tname": "td.name", "rname": "r.name", "rtname": "rtd.name"},
            {"c.ast_id": ref_ast_id},
            joins=[
                Join(("m_type_descriptor", "td"), {"td.type_id": "c.type_id"}, "left"),
                Join(("m_ast", "r"), {"r.ast_id": "c.ref_ast_id"}, "left"),
                Join(("m_type_descriptor", "rtd"), {"rtd.type_id": "r.type_id"}, "left"),
            ],
            order_by=["c.priority"],
            as_dict=True,
        )
        all_rows = list(lvl1)
        compound_ids = [
            int(r["ref_ast_id"])
            for r in lvl1
            if r.get("ref_ast_id") and _category(safe_decode(r.get("rtname")) or safe_decode(r.get("tname"))) == "Compound"
        ]
        if compound_ids:
            lvl2 = reader.select_rows(
                ("m_ast_container", "c"),
                {"ref_ast_id": "c.ref_ast_id", "tname": "td.name", "rname": "r.name", "rtname": "rtd.name"},
                {"c.ast_id": compound_ids},
                joins=[
                    Join(("m_type_descriptor", "td"), {"td.type_id": "c.type_id"}, "left"),
                    Join(("m_ast", "r"), {"r.ast_id": "c.ref_ast_id"}, "left"),
                    Join(("m_type_descriptor", "rtd"), {"rtd.type_id": "r.type_id"}, "left"),
                ],
                order_by=["c.priority"],
                as_dict=True,
            )
            all_rows.extend(lvl2)

        cats = set()
        tag_info: tuple[str, str] | None = None
        for row in all_rows:
            c1 = _category(safe_decode(row.get("tname")))
            c2 = _category(safe_decode(row.get("rtname")))
            rname = (safe_decode(row.get("rname")) or "").strip()
            for c in (c1, c2):
                if c:
                    cats.add(c)
            if rname and not rname.startswith(("struct (", "union (", "enum (")):
                if any(c in ("struct", "structnotbind", "structdecl") for c in (c1, c2)):
                    tag_info = ("struct", rname)
                elif any(c in ("union", "unionnotbind", "uniondecl") for c in (c1, c2)):
                    tag_info = ("union", rname)

        if "pointer" in cats or "functionproto" in cats:
            return (8, 8), None
        if "long" in cats or "longlong" in cats or "double" in cats:
            return (8, 8), None
        if "short" in cats:
            return (2, 2), None
        if "char" in cats or "bool" in cats:
            return (1, 1), None
        if "int" in cats or "float" in cats or "enum" in cats:
            return (4, 4), None
        return None, tag_info

    def _compute_layout(
        self,
        reader: Any,
        vname: str,
        clean_name: str,
        definition: dict[str, Any],
        _seen: frozenset[str] = frozenset(),
        is_union: bool = False,
    ) -> dict[str, Any]:
        fields_raw = self._fetch_container_fields(reader, int(definition["ast_id"]))
        source_field_types, valid_field_names = self._extract_source_field_info(
            vname, definition.get("fname"), definition.get("line_s") or 1, definition.get("line_e") or 1
        )

        # Filter out stray bare forward-tag C_struct / C_union / C_enum AST siblings emitted before a declarator
        filtered_fields: list[dict[str, Any]] = []
        n_raw = len(fields_raw)
        for idx, r in enumerate(fields_raw):
            fname_i = (safe_decode(r.get("field_name")) or "").strip()
            cat_i = _category(safe_decode(r.get("type_name")) or safe_decode(r.get("ref_type_name")) or "")
            has_no_attrs = (
                _opt_int(r.get("attr_size")) is None
                and _opt_int(r.get("attr_offset")) is None
                and _opt_int(r.get("attr_bitfield")) is None
                and not (safe_decode(r.get("attr_type_str")) or "").strip()
            )
            is_anon_agg = fname_i.startswith(("struct (", "union (", "enum ("))
            if cat_i in ("struct", "union", "enum") and has_no_attrs and fname_i and not is_anon_agg:
                is_split_sibling = False
                if idx + 1 < n_raw:
                    nxt = fields_raw[idx + 1]
                    nxt_name = (safe_decode(nxt.get("field_name")) or "").strip()
                    nxt_cat = _category(safe_decode(nxt.get("type_name")) or safe_decode(nxt.get("ref_type_name")) or "")
                    nxt_tstr = (safe_decode(nxt.get("attr_type_str")) or source_field_types.get(nxt_name, "")).strip()
                    if nxt_cat == "Compound" or nxt_tstr.startswith(f"{cat_i} {fname_i}"):
                        is_split_sibling = True
                        if nxt_name and nxt_name not in source_field_types:
                            source_field_types[nxt_name] = f"{cat_i} {fname_i}"
                if is_split_sibling or (valid_field_names and fname_i not in valid_field_names):
                    continue
            filtered_fields.append(r)

        members = []
        curr_offset = 0
        max_align = 1
        total_padding = 0
        holes = 0
        for r in filtered_fields:
            field_name = safe_decode(r["field_name"]) or f"field_{r['priority']}"
            category = _category(safe_decode(r["type_name"]) or safe_decode(r["ref_type_name"]) or "int")
            attr_size = _opt_int(r.get("attr_size"))
            attr_offset = _opt_int(r.get("attr_offset"))
            attr_bitfield = _opt_int(r.get("attr_bitfield"))
            attr_type_str = (safe_decode(r.get("attr_type_str")) or "").strip()
            raw_type_str = source_field_types.get(field_name) or attr_type_str
            _, arr_mult, prim_info, tag_info = _parse_decl_type_info(raw_type_str)

            resolved_align: int | None = None
            if attr_size is not None and attr_size >= 0:
                sz = attr_size
                estimated = False
                if prim_info is not None:
                    resolved_align = prim_info[1]
            else:
                sz = -1
                estimated = True
                ref_ast_id = _opt_int(r.get("ref_ast_id"))
                # 1. Inline struct/union body (e.g. anonymous union inside struct inode)
                if category in ("structdecl", "uniondecl") and ref_ast_id:
                    sub_def = {"ast_id": ref_ast_id, "fname": None, "line_s": 1, "line_e": 1}
                    sub_layout = self._compute_layout(
                        reader, vname, field_name, sub_def, _seen=_seen, is_union=(category == "uniondecl")
                    )
                    if sub_layout["members"]:
                        sz = sub_layout["total_size"] * arr_mult
                        resolved_align = sub_layout["alignment"]
                        estimated = sub_layout["is_estimate"]

                # 2. Known LP64 primitive or pointer from declarator type string
                if sz < 0 and prim_info is not None:
                    sz = prim_info[0] * arr_mult
                    resolved_align = prim_info[1]
                    estimated = False

                # 3. Inspect member AST container children if type string had no primitive/tag info
                if sz < 0 and tag_info is None and ref_ast_id and category in ("unsigned", "signed", "struct", "union", "Compound"):
                    ast_prim, ast_tag = self._inspect_ast_type_tree(reader, ref_ast_id)
                    if ast_prim is not None:
                        sz = ast_prim[0] * arr_mult
                        resolved_align = ast_prim[1]
                        estimated = False
                    elif ast_tag is not None:
                        tag_info = ast_tag
                        if field_name not in source_field_types:
                            source_field_types[field_name] = f"{ast_tag[0]} {ast_tag[1]}"

                # 4. Embedded struct/union member without inline body: resolve against version's global C_structdecl/C_uniondecl
                if sz < 0 and tag_info is not None:
                    tag_kind, tag_name = tag_info
                    if tag_name and tag_name not in _seen:
                        _, target_def = self._definition(reader, vname, tag_name)
                        if target_def is not None and int(target_def["ast_id"]) != int(definition["ast_id"]):
                            target_layout = self._compute_layout(
                                reader,
                                vname,
                                tag_name,
                                target_def,
                                _seen=_seen | {tag_name},
                                is_union=(tag_kind == "union"),
                            )
                            if target_layout["total_size"] > 0:
                                sz = target_layout["total_size"] * arr_mult
                                resolved_align = target_layout["alignment"]
                                estimated = target_layout["is_estimate"]

                if sz < 0:
                    sz, _, estimated = _member_size(category)

            # Alignment derivation
            if resolved_align is not None and resolved_align > 0:
                al = resolved_align
            elif category in _EXACT_SIZES:
                al = _EXACT_SIZES[category][1]
            elif category in _GUESSED_SIZES:
                al = sz if (not estimated and sz in (1, 2, 4, 8)) else _GUESSED_SIZES[category][1]
            else:
                al = min(8, max(1, sz if sz <= 8 and (sz & (sz - 1)) == 0 else 8))
            if sz > 0 and sz < al:
                al = sz
            max_align = max(max_align, al)

            # Offset & padding hole derivation (offset_bits attribute)
            bit_offset = None
            if is_union:
                pad_before = 0
                member_offset = 0
            elif attr_offset is not None and attr_offset >= 0:
                exact_offset_bits = attr_offset
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
                member_offset = curr_offset
            else:
                pad_before = (al - (curr_offset % al)) % al
                if pad_before:
                    holes += 1
                    total_padding += pad_before
                    curr_offset += pad_before
                member_offset = curr_offset

            disp_type = source_field_types.get(field_name) or attr_type_str or _DISPLAY_TYPES.get(category, category)
            members.append({
                "name": field_name,
                "type": disp_type,
                "size": sz,
                "alignment": al,
                "estimated": estimated,
                "offset": member_offset,
                "padding_before": pad_before,
                "cacheline_idx": member_offset // 64,
                "cacheline_offset": member_offset % 64,
                "crosses_cacheline": (member_offset // 64) != ((member_offset + sz - 1) // 64) if sz > 0 else False,
                "bitfield_width": attr_bitfield if attr_bitfield else None,
                "bit_offset": bit_offset,
            })

            if is_union:
                curr_offset = max(curr_offset, sz)
            elif attr_bitfield and attr_bitfield > 0 and attr_offset is not None:
                next_bit = attr_offset + attr_bitfield
                curr_offset = (next_bit + 7) // 8
            else:
                curr_offset += sz

        total_size = reordered_total = 0
        cache_lines_used = 0
        suggested_order: list[str] = []
        if members:
            tail_padding = (max_align - (curr_offset % max_align)) % max_align
            if tail_padding and not is_union:
                holes += 1
            if not is_union:
                total_padding += tail_padding
            total_size = curr_offset + tail_padding
            cache_lines_used = (total_size - 1) // 64 + 1 if total_size > 0 else 0
            if is_union:
                reordered_total = total_size
                suggested_order = [m["name"] for m in members]
            else:
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
            return self._compute_layout(reader, vname, clean_name, definition, _seen=frozenset({clean_name}))
