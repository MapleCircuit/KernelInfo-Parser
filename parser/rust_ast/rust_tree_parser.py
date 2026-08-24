"""parser/rust_ast/rust_tree_parser.py - AST-Tree Parser for rustc Output.

Tokenizes and recursively parses `rustc -Z unpretty=ast-tree` output into
structured `Ast_Rust_*` object trees.
"""
from __future__ import annotations

import re
from typing import Any
from parser.rust_ast.rust_ast_type import (
    Line,
    Ast_Rust,
    Ast_Rust_Struct,
    Ast_Rust_Field,
    Ast_Rust_Enum,
    Ast_Rust_Variant,
    Ast_Rust_Union,
    Ast_Rust_Fn,
    Ast_Rust_Param,
    Ast_Rust_Trait,
    Ast_Rust_TraitAlias,
    Ast_Rust_Impl,
    Ast_Rust_Type,
    Ast_Rust_Const,
    Ast_Rust_Static,
    Ast_Rust_Mod,
    Ast_Rust_Use,
    Ast_Rust_MacroDef,
    Ast_Rust_MacroCall,
    Ast_Rust_ForeignMod,
    Ast_Rust_DocComment,
)


def parse_span_coords(span_str: str) -> tuple[int, int, int, int]:
    """Extract (line_s, line_e, char_s, char_e) from rustc span string."""
    m = re.search(r':(\d+):(\d+):\s*(\d+):(\d+)', span_str)
    if m:
        ls, cs, le, ce = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        return ls, le, cs, ce
    m = re.search(r':(\d+):(\d+):\s*(\d+)', span_str)
    if m:
        ls, cs, ce = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return ls, ls, cs, ce
    m = re.search(r':(\d+):(\d+)', span_str)
    if m:
        ls, cs = int(m.group(1)), int(m.group(2))
        return ls, ls, cs, cs
    return 1, 1, 1, 1


def extract_code_snippet(raw_lines: tuple[str, ...], ls: int, le: int, cs: int, ce: int) -> str:
    """Extract code snippet string from source lines given 1-indexed coordinates."""
    if not raw_lines:
        return ""
    total = len(raw_lines)
    idx_s = max(0, min(total - 1, ls - 1))
    idx_e = max(0, min(total - 1, le - 1))

    if idx_s == idx_e:
        line = raw_lines[idx_s]
        c_start = max(0, min(len(line), cs - 1))
        c_end = max(c_start, min(len(line), ce))
        return line[c_start:c_end]
    else:
        parts = []
        for i in range(idx_s, idx_e + 1):
            line = raw_lines[i]
            if i == idx_s:
                c_start = max(0, min(len(line), cs - 1))
                parts.append(line[c_start:])
            elif i == idx_e:
                c_end = max(0, min(len(line), ce))
                parts.append(line[:c_end])
            else:
                parts.append(line)
        return "\n".join(parts)


def make_line_extent(span_node: Any, raw_lines: tuple[str, ...]) -> Line:
    """Construct a Line object with precise coordinates and source code snippet."""
    span_str = ""
    if isinstance(span_node, dict):
        span_str = span_node.get("span", "")
    elif isinstance(span_node, str):
        span_str = span_node

    ls, le, cs, ce = parse_span_coords(span_str)
    code = extract_code_snippet(raw_lines, ls, le, cs, ce)
    return Line(line_start=ls, line_end=le, char_start=cs, char_end=ce, code=code)


def clean_ident(ident_val: Any) -> str:
    """Clean identifier string by stripping hygiene tags and quotes."""
    if not isinstance(ident_val, str):
        return ""
    ident_val = ident_val.strip()
    if ident_val.startswith('"') and ident_val.endswith('"'):
        ident_val = ident_val[1:-1]
    if "#" in ident_val:
        ident_val = ident_val.split("#")[0]
    return ident_val


def extract_visibility(vis_node: Any) -> str:
    """Extract visibility string from Visibility node."""
    if not isinstance(vis_node, dict):
        return ""
    kind = vis_node.get("kind")
    if kind == "Public":
        return "pub"
    elif isinstance(kind, dict) and kind.get("_type") == "Restricted":
        return "pub(crate)"
    return ""


def extract_type_string(ty_node: Any) -> str:
    """Extract clean type string representation from Ty AST node."""
    if not isinstance(ty_node, dict):
        return str(ty_node or "")

    # If wrapped in Ty { kind: ... }
    kind = ty_node.get("kind")
    if not kind and "_items" in ty_node and isinstance(ty_node["_items"], list) and ty_node["_items"]:
        # sometimes Ty is wrapped in a tuple
        return extract_type_string(ty_node["_items"][0])

    if isinstance(kind, dict):
        k_type = kind.get("_type", "")
        if k_type == "Path":
            path_obj = kind.get("path")
            if not path_obj and "_items" in kind and len(kind["_items"]) > 1:
                path_obj = kind["_items"][1]
            return extract_path_string(path_obj)
        elif k_type in ("Ptr", "Ref"):
            mutbl = "mut " if kind.get("mutbl") == "Mut" or ("_items" in kind and "Mut" in kind["_items"]) else ""
            inner = extract_type_string(kind.get("ty") or (kind["_items"][0] if "_items" in kind and kind["_items"] else None))
            prefix = "&" if k_type == "Ref" else "*"
            return f"{prefix}{mutbl}{inner}"
        elif k_type == "Slice":
            inner = extract_type_string(kind.get("ty") or (kind["_items"][0] if "_items" in kind and kind["_items"] else None))
            return f"[{inner}]"
        elif k_type == "Array":
            inner = extract_type_string(kind.get("ty") or (kind["_items"][0] if "_items" in kind and kind["_items"] else None))
            return f"[{inner}; ...]"
        elif k_type == "Tuple":
            items = kind.get("_items", [])
            return f"({', '.join(extract_type_string(t) for t in items)})"
        elif k_type == "Never":
            return "!"
        elif k_type == "Infer":
            return "_"

    if "path" in ty_node:
        return extract_path_string(ty_node["path"])

    return ""


def extract_path_string(path_node: Any) -> str:
    """Extract path string from Path node."""
    if not isinstance(path_node, dict):
        return str(path_node or "")
    segments = path_node.get("segments", [])
    if isinstance(segments, list):
        segs = []
        for s in segments:
            if isinstance(s, dict):
                ident = clean_ident(s.get("ident"))
                args = s.get("args")
                if isinstance(args, dict) and args.get("_type") == "AngleBracketed":
                    # Generic arguments
                    segs.append(ident)
                else:
                    segs.append(ident)
        return "::".join(segs)
    return ""


class RustAstTreeTokenizer:
    """Fast regular-expression based tokenizer for rustc AST debug output."""

    TOKEN_RE = re.compile(
        r'(?P<STRING>"(?:\\.|[^"\\])*")|'
        r'(?P<SPAN>(?:/[^:\s]+|[a-zA-Z]:\\[^:\s]+|[a-zA-Z0-9_\-\.]+):\d+:\d+(?::\s*\d+:\d+)?\s*\([^)]*\))|'
        r'(?P<LBRACE>\{)|(?P<RBRACE>\})|'
        r'(?P<LBRACKET>\[)|(?P<RBRACKET>\])|'
        r'(?P<LPAREN>\()|(?P<RPAREN>\))|'
        r'(?P<DOUBLE_COLON>::)|(?P<COLON>:)|(?P<COMMA>,)|'
        r'(?P<IDENT>[a-zA-Z_#][a-zA-Z0-9_#$]*)|'
        r'(?P<NUMBER>-?\d+(?:\.\d+)?)|'
        r'(?P<WS>\s+)'
    )

    def __init__(self, text: str) -> None:
        self.tokens: list[tuple[str, str]] = []
        for match in self.TOKEN_RE.finditer(text):
            kind = match.lastgroup
            if kind != "WS":
                self.tokens.append((kind, match.group()))
        self.pos = 0
        self.length = len(self.tokens)

    def peek(self) -> tuple[str | None, str | None]:
        return self.tokens[self.pos] if self.pos < self.length else (None, None)

    def next(self) -> tuple[str | None, str | None]:
        tok = self.peek()
        self.pos += 1
        return tok


class RustAstTreeParser:
    """Recursive-descent parser converting AST debug tokens into Python objects."""

    def __init__(self, tokenizer: RustAstTreeTokenizer) -> None:
        self.tok = tokenizer

    def parse(self) -> Any:
        return self.parse_value()

    def parse_value(self) -> Any:
        kind, val = self.tok.peek()
        if kind is None:
            return None

        if kind == "STRING":
            self.tok.next()
            try:
                return eval(val)
            except Exception:
                return val[1:-1]
        elif kind == "SPAN":
            self.tok.next()
            return {"_type": "Span", "span": val}
        elif kind == "NUMBER":
            self.tok.next()
            return int(val) if "." not in val else float(val)
        elif kind == "LBRACKET":
            return self.parse_list()
        elif kind == "LBRACE":
            return self.parse_dict(None)
        elif kind == "IDENT":
            self.tok.next()
            while self.tok.peek()[0] == "DOUBLE_COLON":
                self.tok.next()
                if self.tok.peek()[0] == "IDENT":
                    val = f"{val}::{self.tok.next()[1]}"
            next_kind, _ = self.tok.peek()
            if next_kind == "LBRACE":
                return self.parse_dict(val)
            elif next_kind == "LPAREN":
                return self.parse_tuple(val)
            else:
                return val
        else:
            self.tok.next()
            return val

    def parse_list(self) -> list[Any]:
        self.tok.next()  # consume [
        items = []
        while self.tok.pos < self.tok.length:
            kind, _ = self.tok.peek()
            if kind == "RBRACKET" or kind is None:
                break
            if kind == "COMMA":
                self.tok.next()
                continue
            items.append(self.parse_value())
            if self.tok.peek()[0] == "COMMA":
                self.tok.next()
            elif self.tok.peek()[0] == "RBRACKET":
                break
        if self.tok.peek()[0] == "RBRACKET":
            self.tok.next()
        return items

    def parse_tuple(self, type_name: str | None) -> dict[str, Any]:
        self.tok.next()  # consume (
        items = []
        while self.tok.pos < self.tok.length:
            kind, _ = self.tok.peek()
            if kind == "RPAREN" or kind is None:
                break
            if kind == "COMMA":
                self.tok.next()
                continue
            items.append(self.parse_value())
            if self.tok.peek()[0] == "COMMA":
                self.tok.next()
            elif self.tok.peek()[0] == "RPAREN":
                break
        if self.tok.peek()[0] == "RPAREN":
            self.tok.next()
        return {"_type": type_name, "_items": items}

    def parse_dict(self, type_name: str | None) -> dict[str, Any]:
        self.tok.next()  # consume {
        d: dict[str, Any] = {"_type": type_name} if type_name else {}
        while self.tok.pos < self.tok.length:
            kind, val = self.tok.peek()
            if kind == "RBRACE" or kind is None:
                break
            if kind == "COMMA":
                self.tok.next()
                continue
            if kind == "IDENT":
                key_name = val
                self.tok.next()
                if self.tok.peek()[0] == "COLON":
                    self.tok.next()  # consume :
                    val_node = self.parse_value()
                    d[key_name] = val_node
                else:
                    d[key_name] = True
            else:
                self.tok.next()
                continue

            if self.tok.peek()[0] == "COMMA":
                self.tok.next()
            elif self.tok.peek()[0] == "RBRACE":
                break
        if self.tok.peek()[0] == "RBRACE":
            self.tok.next()
        return d


def build_rust_ast_nodes(parsed_crate: dict[str, Any], raw_lines: tuple[str, ...]) -> list[Ast_Rust]:
    """Convert parsed Crate dictionary into strongly typed Ast_Rust objects."""
    nodes: list[Ast_Rust] = []

    # 1. Crate-level attributes and doc comments
    for attr in parsed_crate.get("attrs", []):
        if not isinstance(attr, dict):
            continue
        kind = attr.get("kind", {})
        if isinstance(kind, dict) and kind.get("_type") == "DocComment":
            doc_items = kind.get("_items", [])
            doc_text = doc_items[1] if len(doc_items) > 1 else ""
            extent = make_line_extent(attr.get("span"), raw_lines)
            is_inner = attr.get("style") == "Inner"
            nodes.append(Ast_Rust_DocComment(doc_text, extent, is_inner=is_inner))

    # 2. Top-level and nested items
    for item in parsed_crate.get("items", []):
        node = convert_item_node(item, raw_lines)
        if node:
            nodes.append(node)

    return nodes


def convert_item_node(item: dict[str, Any], raw_lines: tuple[str, ...]) -> Ast_Rust | None:
    """Convert an individual Item dict into an Ast_Rust construct."""
    if not isinstance(item, dict):
        return None

    ident = clean_ident(item.get("ident"))
    extent = make_line_extent(item.get("span"), raw_lines)
    vis = extract_visibility(item.get("vis"))

    # Extract doc comments from item attrs
    doc_comments: list[str] = []
    attributes: list[str] = []
    for attr in item.get("attrs", []):
        if not isinstance(attr, dict):
            continue
        kind = attr.get("kind", {})
        if isinstance(kind, dict) and kind.get("_type") == "DocComment":
            doc_items = kind.get("_items", [])
            if len(doc_items) > 1 and isinstance(doc_items[1], str):
                doc_comments.append(doc_items[1].strip())
        else:
            attributes.append(str(kind))

    kind_obj = item.get("kind", {})
    kind_type = kind_obj.get("_type") if isinstance(kind_obj, dict) else kind_obj

    if kind_type == "Struct":
        fields = []
        variant_data = kind_obj.get("_items", [{}])[0] if isinstance(kind_obj.get("_items"), list) else kind_obj
        field_list = []
        if isinstance(variant_data, dict):
            v_type = variant_data.get("_type", "")
            if "Struct" in v_type or "Tuple" in v_type:
                field_list = variant_data.get("_items", [[]])[0] if isinstance(variant_data.get("_items"), list) and variant_data["_items"] else []

        for idx, f in enumerate(field_list):
            if not isinstance(f, dict):
                continue
            f_ident = clean_ident(f.get("ident"))
            f_ty = extract_type_string(f.get("ty"))
            f_extent = make_line_extent(f.get("span"), raw_lines)
            f_vis = extract_visibility(f.get("vis"))
            fields.append(Ast_Rust_Field(f_ident, f_ty, f_extent, vis=f_vis, priority=idx))

        return Ast_Rust_Struct(
            name=ident,
            extent=extent,
            vis=vis,
            fields=fields,
            doc_comments=doc_comments,
            attributes=attributes,
        )

    elif kind_type == "Enum":
        variants = []
        enum_def = kind_obj.get("_items", [{}])[0] if isinstance(kind_obj.get("_items"), list) else kind_obj
        variant_list = enum_def.get("variants", []) if isinstance(enum_def, dict) else []

        for v in variant_list:
            if not isinstance(v, dict):
                continue
            v_ident = clean_ident(v.get("ident"))
            v_extent = make_line_extent(v.get("span"), raw_lines)
            v_fields = []
            v_data = v.get("data", {})
            if isinstance(v_data, dict):
                f_items = v_data.get("_items", [[]])[0] if isinstance(v_data.get("_items"), list) and v_data["_items"] else []
                for idx, f in enumerate(f_items):
                    if isinstance(f, dict):
                        f_ident = clean_ident(f.get("ident"))
                        f_ty = extract_type_string(f.get("ty"))
                        f_ext = make_line_extent(f.get("span"), raw_lines)
                        v_fields.append(Ast_Rust_Field(f_ident, f_ty, f_ext, priority=idx))

            variants.append(Ast_Rust_Variant(
                name=v_ident,
                extent=v_extent,
                fields=v_fields,
            ))

        return Ast_Rust_Enum(
            name=ident,
            extent=extent,
            vis=vis,
            variants=variants,
            doc_comments=doc_comments,
            attributes=attributes,
        )

    elif kind_type == "Union":
        fields = []
        variant_data = kind_obj.get("_items", [{}])[0] if isinstance(kind_obj.get("_items"), list) else kind_obj
        field_list = variant_data.get("_items", [[]])[0] if isinstance(variant_data, dict) and isinstance(variant_data.get("_items"), list) and variant_data["_items"] else []
        for idx, f in enumerate(field_list):
            if isinstance(f, dict):
                f_ident = clean_ident(f.get("ident"))
                f_ty = extract_type_string(f.get("ty"))
                f_extent = make_line_extent(f.get("span"), raw_lines)
                fields.append(Ast_Rust_Field(f_ident, f_ty, f_extent, priority=idx))

        return Ast_Rust_Union(
            name=ident,
            extent=extent,
            vis=vis,
            fields=fields,
            doc_comments=doc_comments,
            attributes=attributes,
        )

    elif kind_type == "Fn":
        fn_data = kind_obj.get("_items", [{}])[0] if isinstance(kind_obj.get("_items"), list) else kind_obj
        sig = fn_data.get("sig", {}) if isinstance(fn_data, dict) else {}
        decl = sig.get("decl", {}) if isinstance(sig, dict) else {}
        inputs = decl.get("inputs", []) if isinstance(decl, dict) else []

        params = []
        for idx, p in enumerate(inputs):
            if not isinstance(p, dict):
                continue
            pat = p.get("pat", {})
            p_ident = clean_ident(pat.get("ident") if isinstance(pat, dict) else "")
            p_ty = extract_type_string(p.get("ty"))
            p_extent = make_line_extent(p.get("span"), raw_lines)
            is_self = p_ident in ("self", "mut self") or "self" in p_ty
            params.append(Ast_Rust_Param(p_ident, p_ty, p_extent, is_self=is_self, priority=idx))

        ret_ty = extract_type_string(decl.get("output")) if isinstance(decl, dict) else ""

        return Ast_Rust_Fn(
            name=ident,
            extent=extent,
            vis=vis,
            params=params,
            ret_type=ret_ty,
            doc_comments=doc_comments,
            attributes=attributes,
        )

    elif kind_type == "Trait":
        trait_data = kind_obj.get("_items", [{}])[0] if isinstance(kind_obj.get("_items"), list) else kind_obj
        items_list = trait_data.get("items", []) if isinstance(trait_data, dict) else []
        trait_items = []
        for assoc in items_list:
            if isinstance(assoc, dict):
                assoc_node = convert_assoc_item(assoc, raw_lines)
                if assoc_node:
                    trait_items.append(assoc_node)

        return Ast_Rust_Trait(
            name=ident,
            extent=extent,
            vis=vis,
            items=trait_items,
            doc_comments=doc_comments,
            attributes=attributes,
        )

    elif kind_type == "TraitAlias":
        return Ast_Rust_TraitAlias(
            name=ident,
            extent=extent,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
        )

    elif kind_type == "Impl":
        impl_data = kind_obj.get("_items", [{}])[0] if isinstance(kind_obj.get("_items"), list) else kind_obj
        self_ty = extract_type_string(impl_data.get("self_ty")) if isinstance(impl_data, dict) else ""
        of_trait_obj = impl_data.get("of_trait") if isinstance(impl_data, dict) else None
        of_trait = extract_path_string(of_trait_obj.get("path") if isinstance(of_trait_obj, dict) else None) if of_trait_obj else None

        items_list = impl_data.get("items", []) if isinstance(impl_data, dict) else []
        impl_items = []
        for assoc in items_list:
            if isinstance(assoc, dict):
                assoc_node = convert_assoc_item(assoc, raw_lines)
                if assoc_node:
                    impl_items.append(assoc_node)

        return Ast_Rust_Impl(
            self_ty=self_ty or ident,
            of_trait=of_trait,
            extent=extent,
            items=impl_items,
            doc_comments=doc_comments,
            attributes=attributes,
        )

    elif kind_type == "Const":
        const_data = kind_obj.get("_items", [{}])[0] if isinstance(kind_obj.get("_items"), list) and kind_obj["_items"] else kind_obj
        ty_obj = const_data.get("ty") if isinstance(const_data, dict) else None
        if not ty_obj and isinstance(kind_obj.get("_items"), list) and len(kind_obj["_items"]) > 1:
            ty_obj = kind_obj["_items"][1]
        ty_str = extract_type_string(ty_obj)
        return Ast_Rust_Const(
            name=ident,
            type_str=ty_str,
            extent=extent,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
        )

    elif kind_type == "Static":
        static_data = kind_obj.get("_items", [{}])[0] if isinstance(kind_obj.get("_items"), list) and kind_obj["_items"] else kind_obj
        ty_obj = static_data.get("ty") if isinstance(static_data, dict) else None
        if not ty_obj and isinstance(kind_obj.get("_items"), list) and kind_obj["_items"]:
            ty_obj = kind_obj["_items"][0]
        ty_str = extract_type_string(ty_obj)
        is_mut = (
            static_data.get("mutability") == "Mut"
            if isinstance(static_data, dict)
            else (len(kind_obj.get("_items", [])) > 1 and kind_obj["_items"][1] == "Mut")
        )
        return Ast_Rust_Static(
            name=ident,
            type_str=ty_str,
            extent=extent,
            is_mut=is_mut,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
        )

    elif kind_type == "TyAlias":
        ty_data = kind_obj.get("_items", [{}])[0] if isinstance(kind_obj.get("_items"), list) else kind_obj
        target_ty = extract_type_string(ty_data.get("ty")) if isinstance(ty_data, dict) else ""
        return Ast_Rust_Type(
            name=ident,
            target_ty=target_ty,
            extent=extent,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
        )

    elif kind_type == "Mod":
        mod_data = kind_obj.get("_items", []) if isinstance(kind_obj.get("_items"), list) else []
        sub_items = []
        for entry in mod_data:
            if isinstance(entry, dict) and entry.get("_type") == "Loaded":
                loaded_items = entry.get("_items", [[]])[0] if entry.get("_items") else []
                for sub in loaded_items:
                    sub_node = convert_item_node(sub, raw_lines)
                    if sub_node:
                        sub_items.append(sub_node)

        return Ast_Rust_Mod(
            name=ident,
            extent=extent,
            vis=vis,
            items=sub_items,
            doc_comments=doc_comments,
            attributes=attributes,
        )

    elif kind_type == "Use":
        use_tree = kind_obj.get("_items", [{}])[0] if isinstance(kind_obj.get("_items"), list) else kind_obj
        prefix = extract_path_string(use_tree.get("prefix") if isinstance(use_tree, dict) else None)
        is_glob = False
        if isinstance(use_tree, dict) and use_tree.get("kind") == "Glob":
            is_glob = True
        return Ast_Rust_Use(
            path=prefix or ident,
            extent=extent,
            vis=vis,
            is_glob=is_glob,
            doc_comments=doc_comments,
            attributes=attributes,
        )

    elif kind_type == "MacroDef":
        return Ast_Rust_MacroDef(
            name=ident,
            extent=extent,
            doc_comments=doc_comments,
            attributes=attributes,
        )

    elif kind_type == "MacCall":
        mac_data = kind_obj.get("_items", [{}])[0] if isinstance(kind_obj.get("_items"), list) else kind_obj
        path = extract_path_string(mac_data.get("path") if isinstance(mac_data, dict) else None)
        return Ast_Rust_MacroCall(
            name=path or ident,
            extent=extent,
            doc_comments=doc_comments,
            attributes=attributes,
        )

    elif kind_type == "ForeignMod":
        mod_data = kind_obj.get("_items", [{}])[0] if isinstance(kind_obj.get("_items"), list) else kind_obj
        abi = str(mod_data.get("abi", "C")) if isinstance(mod_data, dict) else "C"
        items_list = mod_data.get("items", []) if isinstance(mod_data, dict) else []
        foreign_items = []
        for assoc in items_list:
            if isinstance(assoc, dict):
                assoc_node = convert_assoc_item(assoc, raw_lines)
                if assoc_node:
                    foreign_items.append(assoc_node)

        return Ast_Rust_ForeignMod(
            abi=abi,
            extent=extent,
            items=foreign_items,
            doc_comments=doc_comments,
            attributes=attributes,
        )

    return None


def convert_assoc_item(assoc: dict[str, Any], raw_lines: tuple[str, ...]) -> Ast_Rust | None:
    """Convert an associated item inside Trait or Impl into an Ast_Rust node."""
    if not isinstance(assoc, dict):
        return None
    ident = clean_ident(assoc.get("ident"))
    extent = make_line_extent(assoc.get("span"), raw_lines)
    vis = extract_visibility(assoc.get("vis"))

    kind_obj = assoc.get("kind", {})
    kind_type = kind_obj.get("_type") if isinstance(kind_obj, dict) else kind_obj

    if kind_type == "Fn":
        fn_data = kind_obj.get("_items", [{}])[0] if isinstance(kind_obj.get("_items"), list) else kind_obj
        sig = fn_data.get("sig", {}) if isinstance(fn_data, dict) else {}
        decl = sig.get("decl", {}) if isinstance(sig, dict) else {}
        inputs = decl.get("inputs", []) if isinstance(decl, dict) else []

        params = []
        for idx, p in enumerate(inputs):
            if isinstance(p, dict):
                pat = p.get("pat", {})
                p_ident = clean_ident(pat.get("ident") if isinstance(pat, dict) else "")
                p_ty = extract_type_string(p.get("ty"))
                p_extent = make_line_extent(p.get("span"), raw_lines)
                is_self = p_ident in ("self", "mut self") or "self" in p_ty
                params.append(Ast_Rust_Param(p_ident, p_ty, p_extent, is_self=is_self, priority=idx))

        ret_ty = extract_type_string(decl.get("output")) if isinstance(decl, dict) else ""
        return Ast_Rust_Fn(
            name=ident,
            extent=extent,
            vis=vis,
            params=params,
            ret_type=ret_ty,
        )

    elif kind_type == "Const":
        const_data = kind_obj.get("_items", [{}])[0] if isinstance(kind_obj.get("_items"), list) and kind_obj["_items"] else kind_obj
        ty_obj = const_data.get("ty") if isinstance(const_data, dict) else None
        if not ty_obj and isinstance(kind_obj.get("_items"), list) and len(kind_obj["_items"]) > 1:
            ty_obj = kind_obj["_items"][1]
        ty_str = extract_type_string(ty_obj)
        return Ast_Rust_Const(name=ident, type_str=ty_str, extent=extent, vis=vis)

    elif kind_type == "Type":
        ty_data = kind_obj.get("_items", [{}])[0] if isinstance(kind_obj.get("_items"), list) else kind_obj
        target_ty = extract_type_string(ty_data.get("ty")) if isinstance(ty_data, dict) else ""
        return Ast_Rust_Type(name=ident, target_ty=target_ty, extent=extent, vis=vis)

    return None


def parse_rust_ast_tree(ast_debug_text: str, raw_lines: tuple[str, ...]) -> list[Ast_Rust]:
    """Parse raw rustc AST debug text into a list of Ast_Rust objects."""
    if not ast_debug_text or not ast_debug_text.strip():
        return []

    tokenizer = RustAstTreeTokenizer(ast_debug_text)
    parser = RustAstTreeParser(tokenizer)
    tree = parser.parse()

    if isinstance(tree, dict) and tree.get("_type") == "Crate":
        return build_rust_ast_nodes(tree, raw_lines)
    return []
