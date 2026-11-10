"""parser/rust_ast/rust_ast_type.py - Rust AST Node Types and Relational Extractors.

Strongly-typed intermediate representations of Rust constructs (items, their members, doc comments,
attributes and comments) and their mapping into ChangeSet operations (parser/rust_ast/RUST_API.md):

- `extract_ast_node()`: the node and its children as content-addressed `m_ast` rows (`m_ast.view`, Rule 23),
  children as flat `m_ast_container` scalars (Rule 21), `mod x;` declarations joined with `m_ast_include`.
- `extract()`: a top-level construct's tag (tier-1 recycling or a new tag with its transition, Rule 12), its
  `m_bridge_tag` placement and `m_map_ast` map, the symbol definitions it holds and its uses of C symbols
  through bindgen (`bindings::foo`, references to a `Rust_BindingRef` AST named by the symbol; core/SymbolRows.py).

A node's AST only holds what its tag's own text says, so a recycled tag (same code hash) always means the same
AST: outer doc comments and attributes of a top-level item are tagged on their own, never stored in the item.
"""
from __future__ import annotations

from typing import Any

from core.globalstuff import ASTT, OP_REF, REF_POS, SymbolLinkage, compute_code_hash
from core.DBLayout import m_ast, m_ast_container, m_ast_include, m_file_name
from core.SymbolRows import record_def, record_ref, register_tag
from parser.c_ast.tag_tracker import (
    check_exact_match,
    match_prior_tag_transition,
    stage_bridge_tag,
    stage_new_tag,
    stage_tag_map,
)

ChangeSetType = Any

# m_ast.name is VARCHAR(255)
NAME_MAX = 255

# AST types recorded as symbol definitions (m_symbol_def; the webapp outline maps every one of them)
DEF_TYPES = frozenset({
    ASTT.Rust_Fn,
    ASTT.Rust_Struct,
    ASTT.Rust_Enum,
    ASTT.Rust_Union,
    ASTT.Rust_Trait,
    ASTT.Rust_TraitAlias,
    ASTT.Rust_Type,
    ASTT.Rust_Const,
    ASTT.Rust_Static,
    ASTT.Rust_MacroDef,
    ASTT.Rust_Mod,
    ASTT.Rust_Impl,
    ASTT.Rust_Variant,
    ASTT.Rust_Field,
    ASTT.Rust_ModuleAttr,
})


class Line:
    """Coordinates of a source extent (1-based lines, 1-based byte columns, exclusive end) and its text."""

    __slots__ = ("line_pos", "char_pos", "code")

    def __init__(
        self,
        line_start: int | tuple[int, int] | list[int] = 1,
        line_end: int | None = None,
        char_start: int = 1,
        char_end: int = 1,
        code: str = "",
    ) -> None:
        if isinstance(line_start, (tuple, list)):
            self.line_pos = [int(line_start[0]), int(line_start[1])]
        elif line_end is not None:
            self.line_pos = [int(line_start), int(line_end)]
        else:
            self.line_pos = [int(line_start), int(line_start)]

        self.char_pos = [int(char_start), int(char_end)]
        self.code = code

    def grow(self, other: Line) -> None:
        """Expand extent to envelop another Line extent."""
        if (other.line_pos[0], other.char_pos[0]) < (self.line_pos[0], self.char_pos[0]):
            self.line_pos[0], self.char_pos[0] = other.line_pos[0], other.char_pos[0]
        if (other.line_pos[1], other.char_pos[1]) > (self.line_pos[1], self.char_pos[1]):
            self.line_pos[1], self.char_pos[1] = other.line_pos[1], other.char_pos[1]

    def __repr__(self) -> str:
        return f"Line(lines={self.line_pos}, chars={self.char_pos})"


class Ast_Rust:
    """Base class for all Rust AST nodes.

    `name` is the m_ast name (also the tier-2 transition key of a top-level node); `def_name` is the bare
    identifier recorded as a symbol definition (None: not a definition); `linkage` its SymbolLinkage.
    """

    def __init__(
        self,
        name: str = "",
        extent: Line | None = None,
        ast_type: ASTT = ASTT.Undefined,
        vis: str = "",
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
        generics: str = "",
        def_name: str | None = None,
        linkage: int = SymbolLinkage.ExternalDef,
    ) -> None:
        self.name = name
        self.extent = extent or Line(1, 1, 1, 1)
        self.ast_type = ast_type
        self.vis = vis
        self.doc_comments = doc_comments or []
        self.attributes = attributes or []
        self.generics = generics
        self.def_name = def_name
        self.linkage = int(linkage)
        self.children: list[Ast_Rust] = []
        # Repository path of the file a `mod x;` declaration loads (m_ast_include), when it exists
        self.include_target: str | None = None
        # Name the item is linked under for C (`#[no_mangle]`, `#[export]`, `#[export_name = "x"]`), else None
        self.c_symbol: str | None = None
        # Uses of C symbols in a top-level node's text: (name, SymbolRole, line, byte column) (RUST_API.md §7)
        self.binding_refs: list[tuple[str, int, int, int]] = []
        self.ast_ref: Any = None

    def get_symbol_name(self) -> str:
        """Return canonical name string stored in m_ast.name."""
        return self.name

    def extract_ast_node(self, CS: ChangeSetType) -> Any:
        """Extract this AST node and its children into m_ast / m_ast_container, returning its ast_ref."""
        symbol_name = str(self.get_symbol_name())[:NAME_MAX]
        type_id = int(self.ast_type)

        child_refs = [child.extract_ast_node(CS) for child in self.children]
        container_payload: list[Any] = []
        for priority, (child, child_ref) in enumerate(zip(self.children, child_refs)):
            container_payload.extend([None, priority, int(child.ast_type), child_ref])

        include_payload: list[Any] = []
        joins: list[tuple[Any, ...]] = []
        if self.include_target:
            with CS(REF_POS):
                CS.store(m_file_name.get_set(None, self.include_target[:NAME_MAX]))
                fnid_route = CS.get_route_parse()
            joins.append((m_ast.ast_id, m_ast_include.ast_id, 1))
            include_payload = [None, CS.ref(m_file_name.fnid, *fnid_route)]
        if child_refs:
            joins.append((m_ast.ast_id, m_ast_container.ast_id, len(child_refs)))
        if not joins:
            joins.append((m_ast.ast_id,))

        with CS(REF_POS):
            CS.store(m_ast.view(
                tuple(joins),
                None,
                symbol_name,
                type_id,
                *include_payload,
                *container_payload,
            ))
            ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

        self.ast_ref = ast_ref
        return ast_ref

    def record_defs(self, CS: ChangeSetType, tag: Any) -> None:
        """Record the symbol definitions of this node and its members against the enclosing tag."""
        if self.ast_type in DEF_TYPES and self.def_name and self.ast_ref is not None:
            record_def(
                CS,
                tag,
                self.ast_ref,
                str(self.def_name)[:NAME_MAX],
                int(self.ast_type),
                self.linkage,
                self.extent.line_pos[0],
                self.extent.line_pos[1],
            )
            if self.c_symbol and self.c_symbol != self.def_name:
                # `#[export_name = "x"]`: C knows the item as `x`
                record_def(CS, tag, self.ast_ref, str(self.c_symbol)[:NAME_MAX], int(self.ast_type), self.linkage,
                           self.extent.line_pos[0], self.extent.line_pos[1])
        for child in self.children:
            child.record_defs(CS, tag)

    def record_binding_refs(self, CS: ChangeSetType, tag: Any, cache: dict[str, Any]) -> None:
        """Record this node's uses of C symbols, each a reference to the `Rust_BindingRef` AST of its name.

        The AST is a leaf (name, type): the same row for every use of the symbol in every file and version, so
        the webapp finds a symbol's Rust uses by name (`webapp/API.md` §4.3, xref) without resolving them.
        """
        for name, role, line, col in self.binding_refs:
            target = cache.get(name)
            if target is None:
                with CS(REF_POS):
                    CS.store(m_ast.view(((m_ast.ast_id,),), None, name[:NAME_MAX], int(ASTT.Rust_BindingRef)))
                    target = cache[name] = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
            record_ref(CS, tag, target, 0, role, line, col)

    def extract(self, CS: ChangeSetType, binding_cache: dict[str, Any] | None = None) -> Any:
        """Extract this top-level construct: AST, tag (recycled or new), placement, map, definitions and
        references (`binding_cache`: the file's `Rust_BindingRef` ASTs by name, staged once per file)."""
        ast_ref = self.extract_ast_node(CS)
        ext = self.extent
        code_hash = compute_code_hash(ext.code)

        tag = check_exact_match(CS, code_hash) if ext.code else None
        if tag is not None:
            # Tier 1: the prior tag already maps its AST; only its placement in this blob is new
            stage_bridge_tag(CS, tag, ext.line_pos, ext.char_pos)
        else:
            s_tag_id = match_prior_tag_transition(CS, ext, self.get_symbol_name()[:NAME_MAX], self.ast_type)
            tag = stage_new_tag(CS, code_hash, ast_ref, s_tag_id)
            stage_bridge_tag(CS, tag, ext.line_pos, ext.char_pos)
            stage_tag_map(CS, tag, ast_ref, max(1, ext.line_pos[1] - ext.line_pos[0] + 1), ext.char_pos[1])

        register_tag(CS, tag, ext.line_pos[0], ext.line_pos[1], ext.char_pos[0], ext.code)
        self.record_defs(CS, tag)
        if self.binding_refs:
            self.record_binding_refs(CS, tag, {} if binding_cache is None else binding_cache)
        return tag

    def walk(self):
        """This node and every descendant, depth first."""
        yield self
        for child in self.children:
            yield from child.walk()


class Ast_Rust_Signature(Ast_Rust):
    """Header text of an item (`pub const unsafe fn f<T: Copy>(x: T) -> u32 where …`), whitespace-normalized."""

    def __init__(self, text: str, extent: Line | None = None) -> None:
        super().__init__(name=text, extent=extent, ast_type=ASTT.Rust_Signature)


class Ast_Rust_Attribute(Ast_Rust):
    """Outer or inner attribute (`#[derive(Debug)]`, `#![no_std]`, `#[cfg(CONFIG_X86)]`), as written."""

    def __init__(self, text: str, extent: Line | None = None, is_inner: bool = False) -> None:
        super().__init__(name=text, extent=extent, ast_type=ASTT.Rust_Attribute)
        self.is_inner = is_inner


def _with_children(node: Ast_Rust, signature: str | None, attrs: list[Ast_Rust_Attribute] | None, members: list[Ast_Rust]) -> None:
    node.children = ([Ast_Rust_Signature(signature)] if signature else []) + list(attrs or []) + list(members)


class Ast_Rust_Struct(Ast_Rust):
    """Rust struct definition."""

    def __init__(
        self,
        name: str,
        extent: Line,
        vis: str = "",
        generics: str = "",
        fields: list[Ast_Rust_Field] | None = None,
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
        signature: str | None = None,
        attr_nodes: list[Ast_Rust_Attribute] | None = None,
        linkage: int = SymbolLinkage.ExternalDef,
    ) -> None:
        super().__init__(
            name=name,
            extent=extent,
            ast_type=ASTT.Rust_Struct,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
            generics=generics,
            def_name=name,
            linkage=linkage,
        )
        self.fields = fields or []
        _with_children(self, signature, attr_nodes, self.fields)


class Ast_Rust_Field(Ast_Rust):
    """Rust struct, union or variant field: `[vis ]name: Type` (tuple fields use their index)."""

    def __init__(
        self,
        name: str,
        type_str: str,
        extent: Line,
        vis: str = "",
        priority: int = 0,
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
        attr_nodes: list[Ast_Rust_Attribute] | None = None,
    ) -> None:
        label = name if name else str(priority)
        super().__init__(
            name=f"{vis + ' ' if vis else ''}{label}: {type_str}",
            extent=extent,
            ast_type=ASTT.Rust_Field,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
            def_name=name or None,
        )
        self.field_name = name
        self.type_str = type_str
        self.priority = priority
        self.children = list(attr_nodes or [])


class Ast_Rust_Enum(Ast_Rust):
    """Rust enum definition."""

    def __init__(
        self,
        name: str,
        extent: Line,
        vis: str = "",
        generics: str = "",
        variants: list[Ast_Rust_Variant] | None = None,
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
        signature: str | None = None,
        attr_nodes: list[Ast_Rust_Attribute] | None = None,
        linkage: int = SymbolLinkage.ExternalDef,
    ) -> None:
        super().__init__(
            name=name,
            extent=extent,
            ast_type=ASTT.Rust_Enum,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
            generics=generics,
            def_name=name,
            linkage=linkage,
        )
        self.variants = variants or []
        _with_children(self, signature, attr_nodes, self.variants)


class Ast_Rust_Variant(Ast_Rust):
    """Rust enum variant (`Name`, `Name = 5`), its fields as children."""

    def __init__(
        self,
        name: str,
        extent: Line,
        fields: list[Ast_Rust_Field] | None = None,
        discriminant: str | None = None,
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
        attr_nodes: list[Ast_Rust_Attribute] | None = None,
    ) -> None:
        super().__init__(
            name=f"{name} = {discriminant}" if discriminant else name,
            extent=extent,
            ast_type=ASTT.Rust_Variant,
            doc_comments=doc_comments,
            attributes=attributes,
            def_name=name,
        )
        self.variant_name = name
        self.fields = fields or []
        self.discriminant = discriminant
        self.children = list(attr_nodes or []) + list(self.fields)


class Ast_Rust_Union(Ast_Rust):
    """Rust union definition."""

    def __init__(
        self,
        name: str,
        extent: Line,
        vis: str = "",
        generics: str = "",
        fields: list[Ast_Rust_Field] | None = None,
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
        signature: str | None = None,
        attr_nodes: list[Ast_Rust_Attribute] | None = None,
        linkage: int = SymbolLinkage.ExternalDef,
    ) -> None:
        super().__init__(
            name=name,
            extent=extent,
            ast_type=ASTT.Rust_Union,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
            generics=generics,
            def_name=name,
            linkage=linkage,
        )
        self.fields = fields or []
        _with_children(self, signature, attr_nodes, self.fields)


class Ast_Rust_Fn(Ast_Rust):
    """Rust function or method; children: signature, attributes (nested items), parameters."""

    def __init__(
        self,
        name: str,
        extent: Line,
        vis: str = "",
        generics: str = "",
        params: list[Ast_Rust_Param] | None = None,
        ret_type: str = "",
        qualifiers: list[str] | None = None,
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
        signature: str | None = None,
        attr_nodes: list[Ast_Rust_Attribute] | None = None,
        has_body: bool = True,
        linkage: int = SymbolLinkage.ExternalDef,
    ) -> None:
        super().__init__(
            name=name,
            extent=extent,
            ast_type=ASTT.Rust_Fn,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
            generics=generics,
            def_name=name,
            linkage=SymbolLinkage.PrototypeDecl if not has_body else linkage,
        )
        self.params = params or []
        self.ret_type = ret_type
        self.qualifiers = qualifiers or []
        self.has_body = has_body
        self.signature = signature or ""
        _with_children(self, signature, attr_nodes, self.params)


class Ast_Rust_Param(Ast_Rust):
    """Rust function parameter, as written (`x: u32`, `&mut self`, `(a, b): (u8, u8)`, `...`)."""

    def __init__(
        self,
        name: str,
        type_str: str,
        extent: Line,
        is_self: bool = False,
        priority: int = 0,
        text: str | None = None,
    ) -> None:
        if text is None:
            text = f"{name}: {type_str}" if name and type_str and not is_self else (name or type_str)
        super().__init__(name=text, extent=extent, ast_type=ASTT.Rust_Param)
        self.param_name = name
        self.type_str = type_str
        self.is_self = is_self
        self.priority = priority


class Ast_Rust_Trait(Ast_Rust):
    """Rust trait definition."""

    def __init__(
        self,
        name: str,
        extent: Line,
        vis: str = "",
        generics: str = "",
        bounds: list[str] | None = None,
        items: list[Ast_Rust] | None = None,
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
        signature: str | None = None,
        attr_nodes: list[Ast_Rust_Attribute] | None = None,
        linkage: int = SymbolLinkage.ExternalDef,
    ) -> None:
        super().__init__(
            name=name,
            extent=extent,
            ast_type=ASTT.Rust_Trait,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
            generics=generics,
            def_name=name,
            linkage=linkage,
        )
        self.bounds = bounds or []
        self.items = items or []
        _with_children(self, signature, attr_nodes, self.items)


class Ast_Rust_TraitAlias(Ast_Rust):
    """Rust trait alias."""

    def __init__(
        self,
        name: str,
        extent: Line,
        vis: str = "",
        generics: str = "",
        bounds: list[str] | None = None,
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
        signature: str | None = None,
        attr_nodes: list[Ast_Rust_Attribute] | None = None,
        linkage: int = SymbolLinkage.ExternalDef,
    ) -> None:
        super().__init__(
            name=name,
            extent=extent,
            ast_type=ASTT.Rust_TraitAlias,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
            generics=generics,
            def_name=name,
            linkage=linkage,
        )
        self.bounds = bounds or []
        _with_children(self, signature, attr_nodes, [])


class Ast_Rust_Impl(Ast_Rust):
    """Rust impl block, named by its header (`unsafe impl<T: Send> Send for Arc<T>`, `impl Foo`)."""

    def __init__(
        self,
        self_ty: str,
        of_trait: str | None = None,
        extent: Line | None = None,
        generics: str = "",
        items: list[Ast_Rust] | None = None,
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
        header: str | None = None,
        attr_nodes: list[Ast_Rust_Attribute] | None = None,
    ) -> None:
        name = header or (f"impl {of_trait} for {self_ty}" if of_trait else f"impl {self_ty}")
        super().__init__(
            name=name,
            extent=extent or Line(1, 1, 1, 1),
            ast_type=ASTT.Rust_Impl,
            doc_comments=doc_comments,
            attributes=attributes,
            generics=generics,
            def_name=name,
        )
        self.self_ty = self_ty
        self.of_trait = of_trait
        self.items = items or []
        self.children = list(attr_nodes or []) + list(self.items)


class Ast_Rust_Type(Ast_Rust):
    """Rust type alias (`type Foo<T> = Bar<T>`) or associated type (`type Foo: Bound`, `type Foo`)."""

    def __init__(
        self,
        name: str,
        target_ty: str,
        extent: Line,
        vis: str = "",
        generics: str = "",
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
        bounds: str = "",
        attr_nodes: list[Ast_Rust_Attribute] | None = None,
        linkage: int = SymbolLinkage.ExternalDef,
    ) -> None:
        display_name = f"type {name}{generics}"
        if bounds:
            display_name += f": {bounds}"
        if target_ty:
            display_name += f" = {target_ty}"
        super().__init__(
            name=display_name,
            extent=extent,
            ast_type=ASTT.Rust_Type,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
            generics=generics,
            def_name=name,
            linkage=linkage,
        )
        self.type_name = name
        self.target_ty = target_ty
        self.children = list(attr_nodes or [])


class Ast_Rust_Const(Ast_Rust):
    """Rust constant definition (`const NAME: Ty = ...;`)."""

    def __init__(
        self,
        name: str,
        type_str: str,
        extent: Line,
        vis: str = "",
        val_expr: str | None = None,
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
        attr_nodes: list[Ast_Rust_Attribute] | None = None,
        linkage: int = SymbolLinkage.ExternalDef,
    ) -> None:
        super().__init__(
            name=f"const {name}: {type_str}",
            extent=extent,
            ast_type=ASTT.Rust_Const,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
            def_name=name if name != "_" else None,
            linkage=linkage,
        )
        self.const_name = name
        self.type_str = type_str
        self.val_expr = val_expr
        self.children = list(attr_nodes or [])


class Ast_Rust_Static(Ast_Rust):
    """Rust static definition (`static [mut] NAME: Ty = ...;`)."""

    def __init__(
        self,
        name: str,
        type_str: str,
        extent: Line,
        is_mut: bool = False,
        vis: str = "",
        val_expr: str | None = None,
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
        attr_nodes: list[Ast_Rust_Attribute] | None = None,
        linkage: int = SymbolLinkage.ExternalDef,
    ) -> None:
        mut_str = "mut " if is_mut else ""
        super().__init__(
            name=f"static {mut_str}{name}: {type_str}",
            extent=extent,
            ast_type=ASTT.Rust_Static,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
            def_name=name,
            linkage=linkage,
        )
        self.static_name = name
        self.type_str = type_str
        self.is_mut = is_mut
        self.val_expr = val_expr
        self.children = list(attr_nodes or [])


class Ast_Rust_Mod(Ast_Rust):
    """Rust inline module (`mod bar { ... }`) or module declaration (`mod bar;`, linked to its file)."""

    def __init__(
        self,
        name: str,
        extent: Line,
        vis: str = "",
        items: list[Ast_Rust] | None = None,
        is_inline: bool = True,
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
        attr_nodes: list[Ast_Rust_Attribute] | None = None,
        include_target: str | None = None,
        linkage: int = SymbolLinkage.ExternalDef,
    ) -> None:
        super().__init__(
            name=name,
            extent=extent,
            ast_type=ASTT.Rust_Mod,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
            def_name=name,
            linkage=linkage,
        )
        self.mod_name = name
        self.items = items or []
        self.is_inline = is_inline
        self.include_target = include_target
        self.children = list(attr_nodes or []) + list(self.items)


class Ast_Rust_Use(Ast_Rust):
    """Rust use declaration (`use kernel::{prelude::*, sync::Arc as A}`) or `extern crate`."""

    def __init__(
        self,
        path: str,
        extent: Line,
        vis: str = "",
        alias: str | None = None,
        is_glob: bool = False,
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
        tree: str | None = None,
        attr_nodes: list[Ast_Rust_Attribute] | None = None,
    ) -> None:
        if tree is not None:
            name = tree
        elif is_glob:
            name = f"use {path}::*"
        elif alias:
            name = f"use {path} as {alias}"
        else:
            name = f"use {path}"
        super().__init__(
            name=name,
            extent=extent,
            ast_type=ASTT.Rust_Use,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
        )
        self.path = path
        self.alias = alias
        self.is_glob = is_glob
        self.children = list(attr_nodes or [])


class Ast_Rust_MacroDef(Ast_Rust):
    """Rust macro definition (`macro_rules! name` or `macro name`)."""

    def __init__(
        self,
        name: str,
        extent: Line,
        body: str = "",
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
        is_macro_rules: bool = True,
        attr_nodes: list[Ast_Rust_Attribute] | None = None,
    ) -> None:
        super().__init__(
            name=f"macro_rules! {name}" if is_macro_rules else f"macro {name}",
            extent=extent,
            ast_type=ASTT.Rust_MacroDef,
            doc_comments=doc_comments,
            attributes=attributes,
            def_name=name,
            linkage=SymbolLinkage.MacroOrKernelDef,
        )
        self.macro_name = name
        self.body = body
        self.children = list(attr_nodes or [])


class Ast_Rust_MacroCall(Ast_Rust):
    """Rust macro invocation at item level (`module! { ... }`, `kernel::module_platform_driver!`)."""

    def __init__(
        self,
        name: str,
        extent: Line,
        args_str: str = "",
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
        attr_nodes: list[Ast_Rust_Attribute] | None = None,
        module_attrs: list[Ast_Rust_ModuleAttr] | None = None,
    ) -> None:
        super().__init__(
            name=f"{name}!",
            extent=extent,
            ast_type=ASTT.Rust_MacroCall,
            doc_comments=doc_comments,
            attributes=attributes,
        )
        self.macro_name = name
        self.args_str = args_str
        self.module_attrs = module_attrs or []
        self.children = list(attr_nodes or []) + list(self.module_attrs)


class Ast_Rust_ModuleAttr(Ast_Rust):
    """One entry of a module macro (`module!`, `module_platform_driver!`, …): `license: "GPL"`, `type: M`.

    A block entry (`params: { … }`) is named by its key, a typed block (`p: i64 { default: 1 }`, a module
    parameter) by `key: type`; their entries are children. Definitions: the module's `name` (the string's value)
    and each parameter (its name), as `MacroOrKernelDef`.
    """

    def __init__(
        self,
        key: str,
        value: str,
        extent: Line,
        children: list[Ast_Rust_ModuleAttr] | None = None,
        def_name: str | None = None,
    ) -> None:
        super().__init__(
            name=f"{key}: {value}" if value else key,
            extent=extent,
            ast_type=ASTT.Rust_ModuleAttr,
            def_name=def_name,
            linkage=SymbolLinkage.MacroOrKernelDef,
        )
        self.key = key
        self.value = value
        self.children = list(children or [])


class Ast_Rust_ForeignMod(Ast_Rust):
    """Rust foreign module / extern ABI block (`extern "C" { ... }`)."""

    def __init__(
        self,
        abi: str,
        extent: Line,
        items: list[Ast_Rust] | None = None,
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
        is_unsafe: bool = False,
        attr_nodes: list[Ast_Rust_Attribute] | None = None,
    ) -> None:
        super().__init__(
            name=f'{"unsafe " if is_unsafe else ""}extern "{abi}"',
            extent=extent,
            ast_type=ASTT.Rust_ForeignMod,
            doc_comments=doc_comments,
            attributes=attributes,
        )
        self.abi = abi
        self.items = items or []
        self.children = list(attr_nodes or []) + list(self.items)


class Ast_Rust_DocComment(Ast_Rust):
    """Block of consecutive documentation comments (`///`, `//!`, `/** */`), named by its text."""

    def __init__(
        self,
        comment_text: str,
        extent: Line,
        is_inner: bool = False,
    ) -> None:
        super().__init__(
            name=comment_text.strip(),
            extent=extent,
            ast_type=ASTT.Rust_DocComment,
        )
        self.comment_text = comment_text
        self.is_inner = is_inner


class Ast_Rust_Comment(Ast_Rust):
    """Plain comment outside items (consecutive `//` lines, or one `/* ... */`), named by its text."""

    def __init__(self, comment_text: str, extent: Line) -> None:
        super().__init__(name=comment_text.strip(), extent=extent, ast_type=ASTT.Rust_Comment)
        self.comment_text = comment_text
