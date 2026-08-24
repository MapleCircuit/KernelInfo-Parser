"""parser/rust_ast/rust_ast_type.py - Rust AST Node Types and Relational Extractors.

Provides strongly-typed intermediate representations for Rust constructs (structs,
enums, functions, traits, impls, modules, macros, types, constants, statics, doc comments)
and maps them into relational database ChangeSet operations.
"""
from __future__ import annotations

from typing import Any
from core.globalstuff import (
    G,
    ASTT,
    OP_REF,
    REF_ROOT,
    REF_OLD,
    REF_POS,
    REF_NO_REF,
    RouteType,
)
from core.DBLayout import (
    m_v_main,
    m_file_name,
    m_file,
    m_bridge_file,
    m_moved_file,
    m_type_descriptor,
    m_ast,
    m_ast_container,
    m_ast_include,
    m_ast_debug,
    m_tag,
    m_bridge_tag,
    m_map_ast,
    m_bridge_map,
    m_ast_hash,
)

ChangeSetType = Any


class Line:
    """Coordinate tracking for source code extents and snippet content."""

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
        self.line_pos[0] = min(self.line_pos[0], other.line_pos[0])
        self.line_pos[1] = max(self.line_pos[1], other.line_pos[1])
        if self.line_pos[0] == other.line_pos[0]:
            self.char_pos[0] = min(self.char_pos[0], other.char_pos[0])
        if self.line_pos[1] == other.line_pos[1]:
            self.char_pos[1] = max(self.char_pos[1], other.char_pos[1])

    def __repr__(self) -> str:
        return f"Line(lines={self.line_pos}, chars={self.char_pos})"


class Ast_Rust:
    """Base class for all Rust AST nodes."""

    def __init__(
        self,
        name: str = "",
        extent: Line | None = None,
        ast_type: ASTT = ASTT.Undefined,
        vis: str = "",
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
        generics: str = "",
    ) -> None:
        self.name = name
        self.extent = extent or Line(1, 1, 1, 1)
        self.ast_type = ast_type
        self.vis = vis
        self.doc_comments = doc_comments or []
        self.attributes = attributes or []
        self.generics = generics
        self.children: list[Ast_Rust] = []

    def get_symbol_name(self) -> str:
        """Return canonical name string stored in m_ast.name."""
        return self.name

    def extract_ast_node(self, CS: ChangeSetType) -> Any:
        """Extract this AST node and its children into m_ast / m_ast_container, returning its ast_ref."""
        symbol_name = self.get_symbol_name()
        type_id = int(self.ast_type)

        with CS(REF_POS):
            CS.store(m_ast.view(
                ((m_ast.ast_id,),),
                None,
                symbol_name,
                type_id,
            ))
            ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

        for priority, child in enumerate(self.children):
            child_ref = child.extract_ast_node(CS)
            CS.store(m_ast_container.set(
                ast_ref,
                priority,
                int(child.ast_type),
                child_ref,
            ))

        return ast_ref

    def extract(self, CS: ChangeSetType) -> None:
        """Extract this top-level AST construct (AST + tag + bridge_tag + map_ast)."""
        ast_ref = self.extract_ast_node(CS)

        current_tag = (
            None,
            CS.gp.VID,
            0,
            self.extent.code,
            ast_ref,
            self.extent.char_pos[0],
            max(0, self.extent.char_pos[1] - self.extent.char_pos[0]),
        )

        # Prior tag recycling check
        if CS.prior_tags:
            lookup = getattr(CS, "prior_tags_map", None)
            if lookup is not None and self.extent.code in lookup:
                available = [
                    (idx, tid) for idx, tid in lookup[self.extent.code]
                    if idx not in CS.active_tag_list
                ]
                if available:
                    chosen_idx, prior_tag_id = available[0]
                    if isinstance(CS.active_tag_list, set):
                        CS.active_tag_list.add(chosen_idx)
                    else:
                        CS.active_tag_list.append(chosen_idx)

                    CS.store(m_bridge_tag.set(
                        ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
                        prior_tag_id,
                        self.extent.line_pos[0],
                        self.extent.line_pos[1],
                        self.extent.char_pos[0],
                        self.extent.char_pos[1],
                    ))
                    self.map_ast(CS, ast_ref, prior_tag_id, self.extent)
                    return
            else:
                for x, tag in enumerate(CS.prior_tags):
                    if x in CS.active_tag_list:
                        continue
                    if tag[9] == self.extent.code:
                        if isinstance(CS.active_tag_list, set):
                            CS.active_tag_list.add(x)
                        else:
                            CS.active_tag_list.append(x)
                        CS.store(m_bridge_tag.set(
                            ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
                            tag[1],
                            self.extent.line_pos[0],
                            self.extent.line_pos[1],
                            self.extent.char_pos[0],
                            self.extent.char_pos[1],
                        ))
                        self.map_ast(CS, ast_ref, tag[1], self.extent)
                        return

        # New Tag Creation
        with CS(REF_POS):
            CS.store(m_tag.set(*current_tag))
            tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

        # Stage Bridge Tag
        CS.store(m_bridge_tag.set(
            ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
            tag_ref,
            self.extent.line_pos[0],
            self.extent.line_pos[1],
            self.extent.char_pos[0],
            self.extent.char_pos[1],
        ))

        # Stage Spatial Map and Bridge Map
        self.map_ast(CS, ast_ref, tag_ref, self.extent)

    def map_ast(
        self,
        CS: ChangeSetType,
        ast_id_route: RouteType | Any,
        tag_route: RouteType | Any,
        extent: Line | None = None,
    ) -> None:
        """Create m_map_ast and m_bridge_map spatial coordinate entries for this AST node."""
        ext = extent if extent is not None else getattr(self, "extent", None)
        if ext is None:
            return

        line_s = 1
        char_s = 1
        line_e = max(1, ext.line_pos[1] - ext.line_pos[0] + 1)
        char_e = ext.char_pos[1]

        ast_target = (
            ast_id_route
            if (isinstance(ast_id_route, tuple) and len(ast_id_route) == 3 and ast_id_route[1] == OP_REF) or isinstance(ast_id_route, int)
            else CS.ref(m_ast.ast_id, *ast_id_route)
        )
        tag_target = (
            tag_route
            if (isinstance(tag_route, tuple) and len(tag_route) == 3 and tag_route[1] == OP_REF) or isinstance(tag_route, int)
            else CS.ref(m_tag.tag_id, *tag_route)
        )

        CS.store(m_map_ast.set(
            tag_target,
            line_s,
            char_s,
            line_e,
            char_e,
            ast_target,
        ))
        if not hasattr(CS, "register_bridge_map") or CS.register_bridge_map(tag_target, tag_target):
            CS.store(m_bridge_map.set(
                tag_target,
                tag_target,
            ))


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
    ) -> None:
        super().__init__(
            name=name,
            extent=extent,
            ast_type=ASTT.Rust_Struct,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
            generics=generics,
        )
        self.fields = fields or []
        self.children = list(self.fields)


class Ast_Rust_Field(Ast_Rust):
    """Rust struct or tuple field."""

    def __init__(
        self,
        name: str,
        type_str: str,
        extent: Line,
        vis: str = "",
        priority: int = 0,
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
    ) -> None:
        super().__init__(
            name=f"{name}: {type_str}" if name else type_str,
            extent=extent,
            ast_type=ASTT.Rust_Field,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
        )
        self.field_name = name
        self.type_str = type_str
        self.priority = priority


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
    ) -> None:
        super().__init__(
            name=name,
            extent=extent,
            ast_type=ASTT.Rust_Enum,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
            generics=generics,
        )
        self.variants = variants or []
        self.children = list(self.variants)


class Ast_Rust_Variant(Ast_Rust):
    """Rust enum variant."""

    def __init__(
        self,
        name: str,
        extent: Line,
        fields: list[Ast_Rust_Field] | None = None,
        discriminant: str | None = None,
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
    ) -> None:
        super().__init__(
            name=name,
            extent=extent,
            ast_type=ASTT.Rust_Variant,
            doc_comments=doc_comments,
            attributes=attributes,
        )
        self.fields = fields or []
        self.discriminant = discriminant
        self.children = list(self.fields)


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
    ) -> None:
        super().__init__(
            name=name,
            extent=extent,
            ast_type=ASTT.Rust_Union,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
            generics=generics,
        )
        self.fields = fields or []
        self.children = list(self.fields)


class Ast_Rust_Fn(Ast_Rust):
    """Rust function or method declaration/definition."""

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
    ) -> None:
        super().__init__(
            name=name,
            extent=extent,
            ast_type=ASTT.Rust_Fn,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
            generics=generics,
        )
        self.params = params or []
        self.ret_type = ret_type
        self.qualifiers = qualifiers or []
        self.children = list(self.params)


class Ast_Rust_Param(Ast_Rust):
    """Rust function parameter."""

    def __init__(
        self,
        name: str,
        type_str: str,
        extent: Line,
        is_self: bool = False,
        priority: int = 0,
    ) -> None:
        super().__init__(
            name=f"{name}: {type_str}" if name and not is_self else (name or type_str),
            extent=extent,
            ast_type=ASTT.Rust_Param,
        )
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
    ) -> None:
        super().__init__(
            name=name,
            extent=extent,
            ast_type=ASTT.Rust_Trait,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
            generics=generics,
        )
        self.bounds = bounds or []
        self.items = items or []
        self.children = list(self.items)


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
    ) -> None:
        super().__init__(
            name=name,
            extent=extent,
            ast_type=ASTT.Rust_TraitAlias,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
            generics=generics,
        )
        self.bounds = bounds or []


class Ast_Rust_Impl(Ast_Rust):
    """Rust impl block (inherent or trait implementation)."""

    def __init__(
        self,
        self_ty: str,
        of_trait: str | None = None,
        extent: Line | None = None,
        generics: str = "",
        items: list[Ast_Rust] | None = None,
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
    ) -> None:
        name = f"impl {of_trait} for {self_ty}" if of_trait else f"impl {self_ty}"
        super().__init__(
            name=name,
            extent=extent or Line(1, 1, 1, 1),
            ast_type=ASTT.Rust_Impl,
            doc_comments=doc_comments,
            attributes=attributes,
            generics=generics,
        )
        self.self_ty = self_ty
        self.of_trait = of_trait
        self.items = items or []
        self.children = list(self.items)


class Ast_Rust_Type(Ast_Rust):
    """Rust type alias (type Foo = Bar;) or abstract associated type (type Foo;)."""

    def __init__(
        self,
        name: str,
        target_ty: str,
        extent: Line,
        vis: str = "",
        generics: str = "",
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
    ) -> None:
        display_name = f"type {name} = {target_ty}" if target_ty else f"type {name}"
        super().__init__(
            name=display_name,
            extent=extent,
            ast_type=ASTT.Rust_Type,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
            generics=generics,
        )
        self.type_name = name
        self.target_ty = target_ty


class Ast_Rust_Const(Ast_Rust):
    """Rust constant definition (const NAME: Ty = ...;)."""

    def __init__(
        self,
        name: str,
        type_str: str,
        extent: Line,
        vis: str = "",
        val_expr: str | None = None,
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
    ) -> None:
        super().__init__(
            name=f"const {name}: {type_str}",
            extent=extent,
            ast_type=ASTT.Rust_Const,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
        )
        self.const_name = name
        self.type_str = type_str
        self.val_expr = val_expr


class Ast_Rust_Static(Ast_Rust):
    """Rust static definition (static [mut] NAME: Ty = ...;)."""

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
    ) -> None:
        mut_str = "mut " if is_mut else ""
        super().__init__(
            name=f"static {mut_str}{name}: {type_str}",
            extent=extent,
            ast_type=ASTT.Rust_Static,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
        )
        self.static_name = name
        self.type_str = type_str
        self.is_mut = is_mut
        self.val_expr = val_expr


class Ast_Rust_Mod(Ast_Rust):
    """Rust module declaration or inline module definition."""

    def __init__(
        self,
        name: str,
        extent: Line,
        vis: str = "",
        items: list[Ast_Rust] | None = None,
        is_inline: bool = True,
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
    ) -> None:
        super().__init__(
            name=f"mod {name}",
            extent=extent,
            ast_type=ASTT.Rust_Mod,
            vis=vis,
            doc_comments=doc_comments,
            attributes=attributes,
        )
        self.mod_name = name
        self.items = items or []
        self.is_inline = is_inline
        self.children = list(self.items)


class Ast_Rust_Use(Ast_Rust):
    """Rust use declaration / import."""

    def __init__(
        self,
        path: str,
        extent: Line,
        vis: str = "",
        alias: str | None = None,
        is_glob: bool = False,
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
    ) -> None:
        name = f"use {path}"
        if is_glob:
            name = f"use {path}::*"
        elif alias:
            name = f"use {path} as {alias}"
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


class Ast_Rust_MacroDef(Ast_Rust):
    """Rust macro definition (macro_rules! or macro)."""

    def __init__(
        self,
        name: str,
        extent: Line,
        body: str = "",
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
    ) -> None:
        super().__init__(
            name=f"macro_rules! {name}",
            extent=extent,
            ast_type=ASTT.Rust_MacroDef,
            doc_comments=doc_comments,
            attributes=attributes,
        )
        self.macro_name = name
        self.body = body


class Ast_Rust_MacroCall(Ast_Rust):
    """Rust macro invocation (name!(...))."""

    def __init__(
        self,
        name: str,
        extent: Line,
        args_str: str = "",
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
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


class Ast_Rust_ForeignMod(Ast_Rust):
    """Rust foreign module / extern ABI block (extern "C" { ... })."""

    def __init__(
        self,
        abi: str,
        extent: Line,
        items: list[Ast_Rust] | None = None,
        doc_comments: list[str] | None = None,
        attributes: list[str] | None = None,
    ) -> None:
        super().__init__(
            name=f'extern "{abi}"',
            extent=extent,
            ast_type=ASTT.Rust_ForeignMod,
            doc_comments=doc_comments,
            attributes=attributes,
        )
        self.abi = abi
        self.items = items or []
        self.children = list(self.items)


class Ast_Rust_DocComment(Ast_Rust):
    """Rust documentation comment (/// or //!)."""

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
    """Rust general line or block comment."""

    def __init__(
        self,
        comment_text: str,
        extent: Line,
    ) -> None:
        super().__init__(
            name=comment_text.strip(),
            extent=extent,
            ast_type=ASTT.Rust_Comment,
        )
        self.comment_text = comment_text
