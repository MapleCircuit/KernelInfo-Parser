"""parser/kconfig_ast/kconfig_model.py - Finalized Kconfig semantic model (scripts/kconfig port).

Builds the configuration model the kernel's own `conf`/`mconf` tools work on, from the parsed
syntax trees of every Kconfig file reachable from the root Kconfig:

1. Parse actions (zconf.y): menu entries, properties (prompt/default/select/imply/range),
   `depends on` accumulation, `visible if` applied to nested prompts, `option env=`/`modules`,
   `'m'` rewritten to `m && <modules symbol>` in every dependency (menu_check_dep).
2. menu_finalize (menu.c): dependency propagation from parents into every entry and property,
   reverse dependencies from `select`/`imply`, choice typing and membership, implicit submenus
   (entries depending on the preceding symbol), hoisting of `if` blocks and the non-optional
   choice reverse dependency.

Expressions are hash-consed into one table so the shared parent dependency chains stay small when
serialized. Node encoding (ids index the table, -1 means "no expression" = always true):
    ["s", name]      symbol reference (a word: y/m/n, a defined or an undefined symbol)
    ["c", text]      quoted constant
    ["&", a, b] / ["|", a, b] / ["!", a]
    ["=", a, b] / ["!=", a, b] / ["<", a, b] / ["<=", a, b] / [">", a, b] / [">=", a, b]  (a, b are leaves)

The evaluator for this model lives in parser/kconfig_ast/kconfig_eval.py (and its client-side twin
in webapp/frontend/js/views/kconfig/kconfig_engine.js).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from parser.kconfig_ast.kconfig_lexer import KconfigLexer
from parser.kconfig_ast.kconfig_parser import (
    ExprOp,
    KconfigChoice,
    KconfigComment,
    KconfigConfig,
    KconfigExpr,
    KconfigIf,
    KconfigMainmenu,
    KconfigMenu,
    KconfigParser,
    KconfigSource,
    TYPE_BOOL,
    TYPE_HEX,
    TYPE_INT,
    TYPE_STRING,
    TYPE_TRISTATE,
)

MODEL_FORMAT = 2

TYPE_NAMES = {TYPE_BOOL: "bool", TYPE_TRISTATE: "tristate", TYPE_STRING: "string", TYPE_HEX: "hex", TYPE_INT: "int"}

# Legacy node_type codes (m_kconfig_tree): 1=menu, 2=choice, 3=config, 4=menuconfig, 5=comment
NODE_TYPES = {"menu": 1, "choice": 2, "config": 3, "menuconfig": 4, "comment": 5, "if": 6, "root": 0}

_REL_OPS = {
    ExprOp.EQUAL: "=",
    ExprOp.UNEQUAL: "!=",
    ExprOp.LESS: "<",
    ExprOp.LESS_EQUAL: "<=",
    ExprOp.GREATER: ">",
    ExprOp.GREATER_EQUAL: ">=",
}

_VAR_RE = re.compile(r"\$(\w+)")


class ExprTable:
    """Hash-consed expression nodes (structurally equal expressions share one id)."""

    def __init__(self) -> None:
        self.nodes: list[tuple] = []
        self._index: dict[tuple, int] = {}

    def _intern(self, node: tuple) -> int:
        eid = self._index.get(node)
        if eid is None:
            eid = len(self.nodes)
            self.nodes.append(node)
            self._index[node] = eid
        return eid

    def sym(self, name: str) -> int:
        return self._intern(("s", name))

    def const(self, text: str) -> int:
        return self._intern(("c", text))

    def and_(self, a: int | None, b: int | None) -> int | None:
        # expr_alloc_and: a missing operand is dropped (NULL means "yes" in a dependency)
        if a is None:
            return b
        if b is None or a == b:
            return a
        return self._intern(("&", a, b))

    def or_(self, a: int | None, b: int | None) -> int | None:
        # expr_alloc_or: a missing operand is dropped (used to accumulate reverse dependencies)
        if a is None:
            return b
        if b is None or a == b:
            return a
        return self._intern(("|", a, b))

    def not_(self, a: int) -> int:
        return self._intern(("!", a))

    def rel(self, op: str, a: int, b: int) -> int:
        return self._intern((op, a, b))

    def contains_symbol(self, eid: int | None, name: str) -> bool:
        """expr_contains_symbol."""
        if eid is None:
            return False
        node = self.nodes[eid]
        if node[0] == "s":
            return node[1] == name
        if node[0] == "c":
            return False
        return any(self.contains_symbol(child, name) for child in node[1:])

    def depends_symbol(self, eid: int | None, name: str) -> bool:
        """expr_depends_symbol: `name` is a conjunct (FOO, FOO=y, FOO=m or FOO!=n)."""
        if eid is None:
            return False
        node = self.nodes[eid]
        op = node[0]
        if op == "&":
            return self.depends_symbol(node[1], name) or self.depends_symbol(node[2], name)
        if op == "s":
            return node[1] == name
        if op in ("=", "!="):
            left, right = self.nodes[node[1]], self.nodes[node[2]]
            if left == ("s", name):
                if op == "=" and right in (("s", "y"), ("s", "m")):
                    return True
                if op == "!=" and right == ("s", "n"):
                    return True
        return False

    def conjuncts(self, eid: int | None) -> set[int]:
        if eid is None:
            return set()
        node = self.nodes[eid]
        if node[0] == "&":
            return self.conjuncts(node[1]) | self.conjuncts(node[2])
        return {eid}

    def to_string(self, eid: int | None) -> str:
        if eid is None:
            return "y"
        return self._fmt(eid, 0)

    def _fmt(self, eid: int, parent_prec: int) -> str:
        node = self.nodes[eid]
        op = node[0]
        if op == "s":
            return node[1]
        if op == "c":
            return '"' + node[1].replace("\\", "\\\\").replace('"', '\\"') + '"'
        if op == "!":
            return "!" + self._fmt(node[1], 3)
        if op in ("&", "|"):
            prec = 2 if op == "&" else 1
            text = f"{self._fmt(node[1], prec)} {'&&' if op == '&' else '||'} {self._fmt(node[2], prec)}"
            return f"({text})" if prec < parent_prec else text
        return f"{self._fmt(node[1], 4)}{op}{self._fmt(node[2], 4)}"


@dataclass(eq=False)
class _Prop:
    kind: str  # prompt | menu | comment | default | select | imply | range | choice
    text: str | None = None
    expr: Any = None  # default: value expr id; select/imply: target name; range: (min id, max id)
    visible: int | None = None
    menu: "_Menu | None" = None


@dataclass(eq=False)
class _Sym:
    name: str
    type: str = "unknown"
    is_choice: bool = False
    optional: bool = False  # SYMBOL_OPTIONAL: every config symbol, and choices declared `optional`
    auto: bool = False  # SYMBOL_AUTO: env symbols and choices are never written to .config
    env: str | None = None
    choiceval: bool = False
    choice: str | None = None
    members: list[str] = field(default_factory=list)
    props: list[_Prop] = field(default_factory=list)
    rev_dep: int | None = None
    implied: int | None = None
    dir_dep: int | None = None
    menus: list["_Menu"] = field(default_factory=list)
    help: str = ""
    allnoconfig_y: bool = False


@dataclass(eq=False)
class _Menu:
    kind: str  # root | menu | choice | config | menuconfig | comment | if
    sym: _Sym | None = None
    prompt: _Prop | None = None
    dep: int | None = None
    visibility: int | None = None
    parent: "_Menu | None" = None
    children: list["_Menu"] = field(default_factory=list)
    file: str = ""
    line: int = 0
    line_e: int = 0
    help: str = ""
    id: int = 0


@dataclass
class KconfigEnv:
    """Build environment kconfig sees: ARCH/SRCARCH/KERNELVERSION etc. (exported by the top Makefile)."""
    ARCH: str
    SRCARCH: str
    KERNELVERSION: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, str]:
        env = {"ARCH": self.ARCH, "SRCARCH": self.SRCARCH, "KERNELVERSION": self.KERNELVERSION}
        env.update(self.extra)
        return env


class KconfigModelBuilder:
    """Parses a Kconfig tree (via `read_file`) and finalizes it exactly like scripts/kconfig."""

    def __init__(self, read_file: Callable[[str], str | None], env: KconfigEnv, preprocessor: Any = None) -> None:
        self.read_file = read_file
        self.env = env.as_dict()
        # Linux 4.18+ macro language (kconfig_preprocess.KconfigPreprocessor); None for older trees
        self.pp = preprocessor
        self.exprs = ExprTable()
        self.symbols: dict[str, _Sym] = {}
        self.choices: dict[str, _Sym] = {}
        self.root = _Menu(kind="root")
        self.root.prompt = _Prop(kind="menu", text="Linux Kernel Configuration", menu=self.root)
        self.modules_name: str | None = None
        self.defconfig_list: str | None = None
        self.warnings: list[str] = []
        self.files: list[str] = []
        # Direct references made by each entry's own properties: name -> [{kind, by, file, line}]
        self.references: dict[str, list[dict[str, Any]]] = {}
        self._anon_choices = 0
        self._parse_cache: dict[str, list[Any]] = {}
        self._mod_rewrites: list[tuple[str, int]] = []

    # ------------------------------------------------------------------ lookup helpers
    def _lookup(self, name: str) -> _Sym:
        sym = self.symbols.get(name)
        if sym is None:
            sym = self.symbols[name] = _Sym(name=name)
        return sym

    def _set_type(self, sym: _Sym, type_name: str, where: str) -> None:
        # menu_set_type: the first definition wins, a conflicting later one only warns
        if sym.type == type_name:
            return
        if sym.type == "unknown":
            sym.type = type_name
            return
        self.warnings.append(f"{where}: type of '{sym.name}' redefined from '{sym.type}' to '{type_name}'")

    def _expand(self, text: str) -> str:
        return _VAR_RE.sub(lambda m: self.env.get(m.group(1), ""), text)

    # ------------------------------------------------------------------ expressions
    def _conv(self, expr: KconfigExpr | None, check_dep: bool) -> int | None:
        """Convert a parser expression; check_dep applies menu_check_dep ('m' -> 'm && MODULES')."""
        if expr is None:
            return None
        op = expr.op
        if op == ExprOp.SYMBOL_REF:
            eid = self.exprs.sym(expr.value or "")
            if check_dep and expr.value == "m":
                # The modules symbol is only known once every file is parsed: patched in build()
                placeholder = self.exprs.sym("\0MODULES")
                eid = self.exprs.and_(eid, placeholder)
            return eid
        if op == ExprOp.CONSTANT:
            text = expr.value or ""
            if text.startswith("$("):
                self.warnings.append(f"unevaluated preprocessor macro {text}")
            return self.exprs.const(text)
        if op == ExprOp.NOT:
            return self.exprs.not_(self._conv(expr.left, check_dep))
        if op in (ExprOp.AND, ExprOp.OR):
            left = self._conv(expr.left, check_dep)
            right = self._conv(expr.right, check_dep)
            return self.exprs.and_(left, right) if op == ExprOp.AND else self.exprs.or_(left, right)
        if op in _REL_OPS:
            # Operands of comparisons are plain symbols, never rewritten by menu_check_dep
            return self.exprs.rel(_REL_OPS[op], self._conv(expr.left, False), self._conv(expr.right, False))
        return None

    def _collect_refs(self, expr: KconfigExpr | None, out: set[str]) -> None:
        if expr is None:
            return
        if expr.op == ExprOp.SYMBOL_REF and expr.value and expr.value not in ("y", "m", "n"):
            out.add(expr.value)
        self._collect_refs(expr.left, out)
        self._collect_refs(expr.right, out)

    def _add_ref(self, target: str, kind: str, by: str, file: str, line: int) -> None:
        self.references.setdefault(target, []).append({"kind": kind, "by": by, "file": file, "line": line})

    # ------------------------------------------------------------------ parse actions (zconf.y)
    def _parse(self, path: str, stack: list[str]) -> list[Any] | None:
        """Parse a file; files it sources are parsed in place (KconfigSource.items), in kconfig order."""
        if self.pp is None and path in self._parse_cache:
            return self._parse_cache[path]
        content = self.read_file(path)
        if content is None:
            return None
        if path not in self.files:
            self.files.append(path)
        inner = stack + [path]

        def on_source(src: KconfigSource) -> list[Any] | None:
            target = src.path if self.pp is not None else self._expand(src.path)
            if src.is_rsource:
                target = os.path.normpath(os.path.join(os.path.dirname(path), target))
            src.resolved_path = target
            where = f"{path}:{src.line_s}"
            if target in inner:
                self.warnings.append(f"{where}: recursive inclusion of {target} ignored")
                return None
            items = self._parse(target, inner)
            if items is None and not src.is_osource:
                self.warnings.append(f"{where}: can't open file \"{target}\"")
            return items

        lexer = KconfigLexer(content, self.pp, path)
        items = KconfigParser(lexer.iter_tokens(), content, on_source=on_source).parse()
        if self.pp is None:
            self._parse_cache[path] = items
        return items

    def _add_entry(self, parent: _Menu, kind: str, sym: _Sym | None, file: str, item: Any) -> _Menu:
        menu = _Menu(kind=kind, sym=sym, parent=parent, file=file, line=item.line_s, line_e=item.line_e)
        parent.children.append(menu)
        if sym is not None:
            sym.menus.append(menu)
        return menu

    def _add_prop(self, menu: _Menu, kind: str, text: str | None, expr: Any, cond: KconfigExpr | None) -> _Prop:
        prop = _Prop(kind=kind, text=text, expr=expr, visible=self._conv(cond, True), menu=menu)
        if menu.sym is not None:
            menu.sym.props.append(prop)
        if text is not None:
            if kind == "prompt":
                # Apply all upper menus' `visible if` to the actual prompt
                upper = menu.parent
                while upper is not None:
                    if upper.visibility is not None:
                        prop.visible = self.exprs.and_(prop.visible, upper.visibility)
                    upper = upper.parent
            menu.prompt = prop
        return prop

    def load(self, root_file: str) -> None:
        items = self._parse(root_file, [])
        if items is None:
            self.warnings.append(f"can't open root Kconfig \"{root_file}\"")
            return
        self._items(items, self.root, root_file, [])

    def _items(self, items: list[Any], parent: _Menu, file: str, stack: list[str]) -> None:
        for item in items:
            if isinstance(item, KconfigConfig):
                self._config(item, parent, file)
            elif isinstance(item, KconfigMenu):
                menu = self._add_entry(parent, "menu", None, file, item)
                self._add_prop(menu, "menu", item.title, None, item.prompt_cond)
                menu.visibility = self._conv(item.visible_if, False)
                for dep in item.depends_on:
                    menu.dep = self.exprs.and_(menu.dep, self._conv(dep, True))
                menu.help = item.help_text or ""
                self._items(item.children, menu, file, stack)
            elif isinstance(item, KconfigChoice):
                self._choice(item, parent, file, stack)
            elif isinstance(item, KconfigIf):
                menu = self._add_entry(parent, "if", None, file, item)
                menu.dep = self._conv(item.cond, True)
                self._items(item.children, menu, file, stack)
            elif isinstance(item, KconfigComment):
                menu = self._add_entry(parent, "comment", None, file, item)
                self._add_prop(menu, "comment", item.title, None, None)
                for dep in item.depends_on:
                    menu.dep = self.exprs.and_(menu.dep, self._conv(dep, True))
            elif isinstance(item, KconfigSource):
                if item.items is not None:
                    self._items(item.items, parent, item.resolved_path, stack)
            elif isinstance(item, KconfigMainmenu):
                self.root.prompt = _Prop(kind="menu", text=self._expand(item.title), menu=self.root)
                self.root.file, self.root.line = file, item.line_s

    def _config(self, cfg: KconfigConfig, parent: _Menu, file: str) -> None:
        where = f"{file}:{cfg.line_s}"
        if cfg.name in ("y", "m", "n") or not cfg.name:
            self.warnings.append(f"{where}: invalid symbol name '{cfg.name}'")
            return
        sym = self._lookup(cfg.name)
        sym.optional = True
        menu = self._add_entry(parent, "menuconfig" if cfg.is_menuconfig else "config", sym, file, cfg)
        if cfg.sym_type in TYPE_NAMES:
            self._set_type(sym, TYPE_NAMES[cfg.sym_type], where)
        for dep in cfg.depends_on:
            menu.dep = self.exprs.and_(menu.dep, self._conv(dep, True))
        if cfg.prompt:
            self._add_prop(menu, "prompt", cfg.prompt, None, cfg.prompt_cond)
        refs: set[str] = set()
        for dep in cfg.depends_on:
            self._collect_refs(dep, refs)
        for name in refs:
            self._add_ref(name, "depends_on", cfg.name, file, cfg.line_s)
        for value, cond in cfg.defaults:
            self._add_prop(menu, "default", None, self._conv(value, False), cond)
            vrefs: set[str] = set()
            self._collect_refs(value, vrefs)
            self._collect_refs(cond, vrefs)
            for name in vrefs:
                self._add_ref(name, "default", cfg.name, file, cfg.line_s)
        for kind, entries in (("select", cfg.selects), ("imply", cfg.implies)):
            for target, cond in entries:
                self._lookup(target)
                self._add_prop(menu, kind, None, target, cond)
                self._add_ref(target, kind, cfg.name, file, cfg.line_s)
                crefs: set[str] = set()
                self._collect_refs(cond, crefs)
                for name in crefs:
                    self._add_ref(name, f"{kind}_condition", cfg.name, file, cfg.line_s)
        for lo, hi, cond in cfg.ranges:
            self._add_prop(menu, "range", None, (self._conv(lo, False), self._conv(hi, False)), cond)
        if cfg.prompt_cond is not None:
            prefs: set[str] = set()
            self._collect_refs(cfg.prompt_cond, prefs)
            for name in prefs:
                self._add_ref(name, "prompt_condition", cfg.name, file, cfg.line_s)
        if cfg.env:
            # prop_add_env: env symbols are automatic and default to the environment value
            sym.auto = True
            sym.env = cfg.env
            if cfg.env in self.env:
                sym.props.append(_Prop(kind="default", expr=self.exprs.const(self.env[cfg.env])))
            else:
                self.warnings.append(f"{where}: environment variable {cfg.env} undefined")
        if cfg.is_modules and self.modules_name is None:
            self.modules_name = cfg.name
        if cfg.is_defconfig_list and self.defconfig_list is None:
            self.defconfig_list = cfg.name
        if cfg.allnoconfig_y:
            sym.allnoconfig_y = True
        if cfg.help_text:
            menu.help = cfg.help_text
            if not sym.help:
                sym.help = cfg.help_text

    def _choice(self, item: KconfigChoice, parent: _Menu, file: str, stack: list[str]) -> None:
        if item.name:
            key = f"<choice:{item.name}>"  # choices live in their own namespace (SYMBOL_CHOICE)
        else:
            self._anon_choices += 1
            key = f"<choice-{self._anon_choices}>"
        sym = self.choices.get(key)
        if sym is None:
            sym = self.choices[key] = _Sym(name=key, is_choice=True, auto=True)
        menu = self._add_entry(parent, "choice", sym, file, item)
        sym.props.append(_Prop(kind="choice", menu=menu))
        if item.sym_type in (TYPE_BOOL, TYPE_TRISTATE):
            self._set_type(sym, TYPE_NAMES[item.sym_type], f"{file}:{item.line_s}")
        if item.is_optional:
            sym.optional = True
        for dep in item.depends_on:
            menu.dep = self.exprs.and_(menu.dep, self._conv(dep, True))
        if item.prompt:
            self._add_prop(menu, "prompt", item.prompt, None, item.prompt_cond)
        for target, cond in item.defaults:
            self._lookup(target)
            self._add_prop(menu, "default", None, self.exprs.sym(target), cond)
        if item.help_text:
            menu.help = item.help_text
            if not sym.help:
                sym.help = item.help_text
        self._items(item.children, menu, file, stack)

    # ------------------------------------------------------------------ menu_finalize (menu.c)
    def _sym_type(self, name: str) -> str:
        sym = self.symbols.get(name) or self.choices.get(name)
        return sym.type if sym else "unknown"

    def _trans_bool(self, eid: int | None) -> int | None:
        """expr_trans_bool: FOO!=n -> FOO for tristate FOO (applied to non-tristate owners)."""
        if eid is None:
            return None
        node = self.exprs.nodes[eid]
        op = node[0]
        if op in ("&", "|"):
            left, right = self._trans_bool(node[1]), self._trans_bool(node[2])
            return self.exprs.and_(left, right) if op == "&" else self.exprs.or_(left, right)
        if op == "!":
            return self.exprs.not_(self._trans_bool(node[1]))
        if op == "!=":
            left, right = self.exprs.nodes[node[1]], self.exprs.nodes[node[2]]
            if left[0] == "s" and right == ("s", "n") and self._sym_type(left[1]) == "tristate":
                return node[1]
        return eid

    def _menu_props(self, menu: _Menu) -> list[_Prop]:
        if menu.sym is not None:
            return [p for p in menu.sym.props if p.menu is menu]
        return [menu.prompt] if menu.prompt is not None else []

    def _finalize(self, parent: _Menu) -> None:
        sym = parent.sym
        ex = self.exprs
        if parent.children:
            if sym is not None and sym.is_choice:
                if sym.type == "unknown":
                    for menu in parent.children:
                        if menu.sym is not None and menu.sym.type != "unknown":
                            sym.type = menu.sym.type
                            break
                for menu in parent.children:
                    if menu.sym is not None and menu.sym.type == "unknown":
                        menu.sym.type = sym.type
                parentdep = ex.sym(sym.name)
            elif parent.prompt is not None:
                parentdep = parent.prompt.visible
            else:
                parentdep = parent.dep

            for menu in parent.children:
                basedep = ex.and_(parentdep, menu.dep)
                menu.dep = basedep
                for prop in self._menu_props(menu):
                    dep = ex.and_(basedep, prop.visible)
                    if menu.sym is not None and menu.sym.type != "tristate":
                        dep = self._trans_bool(dep)
                    prop.visible = dep
                    if prop.kind in ("select", "imply") and menu.sym is not None:
                        target = self._lookup(prop.expr)
                        term = ex.and_(ex.sym(menu.sym.name), dep)
                        if prop.kind == "select":
                            target.rev_dep = ex.or_(target.rev_dep, term)
                        else:
                            target.implied = ex.or_(target.implied, term)
            # Children may adopt their following siblings (implicit submenus): walk by index
            idx = 0
            while idx < len(parent.children):
                self._finalize(parent.children[idx])
                idx += 1
        elif sym is not None:
            self._implicit_submenu(parent, sym)
            sym.dir_dep = ex.or_(sym.dir_dep, parent.dep) if sym.dir_dep is not None else parent.dep

        idx = 0
        while idx < len(parent.children):
            menu = parent.children[idx]
            if sym is not None and sym.is_choice and menu.sym is not None and not menu.sym.choiceval:
                member = menu.sym
                member.choiceval = True
                member.choice = sym.name
                if not menu.prompt:
                    self.warnings.append(f"{menu.file}:{menu.line}: choice value must have a prompt")
                if sym.type == "tristate" and member.type != "tristate":
                    # Non-tristate values of tristate choices depend on the choice being y
                    basedep = ex.rel("=", ex.sym(sym.name), ex.sym("y"))
                    menu.dep = ex.and_(basedep, menu.dep)
                    for prop in self._menu_props(menu):
                        prop.visible = ex.and_(basedep, prop.visible)
                sym.members.append(member.name)
            if menu.children and (menu.prompt is None or not menu.prompt.text):
                # Entries without prompt (if-blocks) pass their children up to this level
                for child in menu.children:
                    child.parent = parent
                parent.children[idx + 1:idx + 1] = menu.children
                menu.children = []
            idx += 1

        if sym is not None and not sym.optional and parent.prompt is not None:
            sym.rev_dep = ex.or_(sym.rev_dep, ex.and_(parent.prompt.visible, ex.sym("m")))

    def _implicit_submenu(self, parent: _Menu, sym: _Sym) -> None:
        """Following siblings whose dependency names this symbol become its children (menuconfig rule)."""
        ex = self.exprs
        holder = parent.parent
        if holder is None:
            return
        siblings = holder.children
        try:
            pos = next(i for i, m in enumerate(siblings) if m is parent)
        except StopIteration:
            return
        basedep = parent.prompt.visible if parent.prompt is not None else None
        base_terms = ex.conjuncts(basedep)
        grabbed: list[_Menu] = []
        cursor = pos + 1
        while cursor < len(siblings):
            menu = siblings[cursor]
            dep = menu.prompt.visible if menu.prompt is not None else menu.dep
            if not ex.contains_symbol(dep, sym.name):
                break
            if not ex.depends_symbol(dep, sym.name):
                # Approximates expr_eliminate_eq: every conjunct of the parent's visibility must recur
                if not base_terms <= ex.conjuncts(dep):
                    break
            self._finalize(menu)
            menu.parent = parent
            grabbed.append(menu)
            cursor = next(i for i, m in enumerate(siblings) if m is menu) + 1
        if grabbed:
            first = next(i for i, m in enumerate(siblings) if m is grabbed[0])
            last = next(i for i, m in enumerate(siblings) if m is grabbed[-1])
            parent.children = siblings[first:last + 1]
            del siblings[first:last + 1]

    # ------------------------------------------------------------------ output
    def build(self, root_file: str) -> dict[str, Any]:
        self.load(root_file)
        modules = self.modules_name or "MODULES"
        self._patch_modules(modules)
        self._finalize(self.root)

        menus_out: list[dict[str, Any]] = []
        counter = 0

        def number(menu: _Menu) -> None:
            nonlocal counter
            menu.id = counter
            counter += 1
            for child in menu.children:
                number(child)

        number(self.root)

        def emit(menu: _Menu) -> None:
            entry: dict[str, Any] = {
                "tree_id": menu.id,
                "parent_id": menu.parent.id if menu.parent is not None else -1,
                "kind": menu.kind,
                "node_type": NODE_TYPES[menu.kind],
                "symbol_name": menu.sym.name if menu.sym is not None else None,
                "prompt": menu.prompt.text if menu.prompt is not None else None,
                "pvis": _eid(menu.prompt.visible) if menu.prompt is not None else -1,
                "vis_if": _eid(menu.visibility),
                "dep": _eid(menu.dep),
                "file_path": menu.file,
                "line_s": menu.line,
                "line_e": menu.line_e,
                "has_help": bool(menu.help),
                "children": [c.id for c in menu.children],
            }
            entry["title"] = entry["prompt"] or entry["symbol_name"] or ""
            menus_out.append(entry)
            for child in menu.children:
                emit(child)

        emit(self.root)

        symbols_out: dict[str, Any] = {}
        for sym in list(self.symbols.values()) + list(self.choices.values()):
            if not sym.menus and not sym.props:
                continue  # referenced only: undefined symbol
            entry = {
                "type": sym.type,
                "prompts": [_eid(p.visible) for p in sym.props if p.text],
                "defaults": [[_eid(p.expr), _eid(p.visible)] for p in sym.props if p.kind == "default"],
                "ranges": [[_eid(p.expr[0]), _eid(p.expr[1]), _eid(p.visible)] for p in sym.props if p.kind == "range"],
                "selects": [[p.expr, _eid(p.visible)] for p in sym.props if p.kind == "select"],
                "implies": [[p.expr, _eid(p.visible)] for p in sym.props if p.kind == "imply"],
                "rev_dep": _eid(sym.rev_dep),
                "implied": _eid(sym.implied),
                "dir_dep": _eid(sym.dir_dep),
                "menus": [m.id for m in sym.menus],
            }
            if sym.is_choice:
                entry["is_choice"] = True
                entry["optional"] = sym.optional
                entry["members"] = sym.members
            if sym.choiceval:
                entry["choice"] = sym.choice
            if sym.auto:
                entry["auto"] = True
            if sym.env:
                entry["env"] = sym.env
            if sym.allnoconfig_y:
                entry["allnoconfig_y"] = True
            symbols_out[sym.name] = entry

        return {
            "format": MODEL_FORMAT,
            "semantics": self.conf_semantics(),
            "config_header": self.config_header(),
            "env": self.env,
            "root_kconfig": root_file,
            "mainmenu": self.root.prompt.text if self.root.prompt else "",
            "modules_sym": modules,
            "defconfig_list": self.defconfig_list,
            "exprs": [list(node) for node in self.exprs.nodes],
            "symbols": symbols_out,
            "nodes": menus_out,
            "files": self.files,
            "warnings": self.warnings[:200],
        }

    def conf_semantics(self) -> dict[str, bool]:
        """conf behaviours that changed over time, detected from the revision's scripts/kconfig."""
        symbol_c = self.read_file("scripts/kconfig/symbol.c") or ""
        confdata_c = self.read_file("scripts/kconfig/confdata.c") or ""
        return {
            # Only defaults evaluating to y/m mark a symbol as written (later 4.x+)
            "write_on_nonzero_default": re.search(r"if \(newval\.tri != no\)\s*sym->flags \|= SYMBOL_WRITE", symbol_c) is not None,
            # "# end of <menu>" lines in .config (5.x+)
            "menu_end_comments": "# end of %s" in confdata_c,
            # int/hex symbols without value default to "0" / "0x0" instead of ""
            "int_hex_zero_default": 'newval.val = "0x0"' in symbol_c,
            # m-visible tristate values of a y choice are hidden
            "tristate_choice_mod_hidden": "choice_sym->curr.tri == yes" in symbol_c,
        }

    def config_header(self) -> str:
        confdata_c = self.read_file("scripts/kconfig/confdata.c") or ""
        if "Automatically generated make config: don't edit" in confdata_c or not confdata_c:
            return "Automatically generated make config: don't edit"
        return "Automatically generated file; DO NOT EDIT."

    def _patch_modules(self, modules: str) -> None:
        """Replace the '\\0MODULES' placeholder leaf now that the modules symbol is known."""
        placeholder = self.exprs._index.get(("s", "\0MODULES"))
        if placeholder is None:
            return
        # Placeholder leaves are rewritten in place; hash-consing keys are refreshed accordingly
        self.exprs.nodes[placeholder] = ("s", modules)
        del self.exprs._index[("s", "\0MODULES")]
        self.exprs._index.setdefault(("s", modules), placeholder)

    def help_texts(self) -> dict[str, str]:
        return {name: sym.help for name, sym in self.symbols.items() if sym.help}

    def menu_help(self) -> dict[int, str]:
        out: dict[int, str] = {}

        def walk(menu: _Menu) -> None:
            if menu.help:
                out[menu.id] = menu.help
            for child in menu.children:
                walk(child)

        walk(self.root)
        return out


def _eid(eid: int | None) -> int:
    return -1 if eid is None else eid


def parse_make_assignments(makefile: str) -> dict[str, str]:
    """VERSION/PATCHLEVEL/SUBLEVEL/EXTRAVERSION from the top Makefile."""
    values: dict[str, str] = {}
    for line in makefile.splitlines()[:40]:
        m = re.match(r"^(VERSION|PATCHLEVEL|SUBLEVEL|EXTRAVERSION)\s*=\s*(.*?)\s*$", line)
        if m:
            values[m.group(1)] = m.group(2)
    return values


def kernel_version_string(makefile: str) -> str:
    """$(VERSION)$(if $(PATCHLEVEL),.$(PATCHLEVEL)$(if $(SUBLEVEL),.$(SUBLEVEL)))$(EXTRAVERSION)."""
    v = parse_make_assignments(makefile)
    out = v.get("VERSION", "")
    if v.get("PATCHLEVEL"):
        out += "." + v["PATCHLEVEL"]
        if v.get("SUBLEVEL"):
            out += "." + v["SUBLEVEL"]
    return out + v.get("EXTRAVERSION", "")


def arch_aliases(makefile: str) -> dict[str, str]:
    """ARCH values the top Makefile maps onto another SRCARCH (`ifeq ($(ARCH),i386) SRCARCH := x86`)."""
    aliases: dict[str, str] = {}
    current: str | None = None
    for line in makefile.splitlines():
        m = re.match(r"^\s*ifeq\s*\(\s*\$\(ARCH\)\s*,\s*([\w-]+)\s*\)", line)
        if m:
            current = m.group(1)
            continue
        if current:
            a = re.match(r"^\s*SRCARCH\s*:?=\s*([\w-]+)\s*$", line)
            if a:
                aliases[current] = a.group(1)
                continue
        if re.match(r"^\s*endif\b", line):
            current = None
    return aliases


def resolve_kbuild_defconfig(arch_makefile: str, arch: str, srcarch: str | None = None) -> str | None:
    """KBUILD_DEFCONFIG from arch/$SRCARCH/Makefile as `make ARCH=<arch> defconfig` sees it.

    Conditionals are evaluated with ARCH/SRCARCH known, every CONFIG_* undefined (no .config is
    read for configuration targets) and CROSS_COMPILE set (configuring for another machine);
    conditions on any other variable are unknown and both branches are considered.
    """
    known = {"ARCH": arch, "SRCARCH": srcarch or arch, "CROSS_COMPILE": "cross-"}

    def subst(text: str) -> str | None:
        unknown = False

        def repl(m: re.Match) -> str:
            nonlocal unknown
            name = m.group(1)
            if name.startswith("CONFIG_"):
                return ""
            if name in known:
                return known[name]
            unknown = True
            return ""

        out = re.sub(r"\$\((\w+)\)", repl, text)
        return None if unknown or "$(" in out else out

    def condition(kind: str, args: str) -> bool | None:
        if kind in ("ifdef", "ifndef"):
            name = args.strip()
            value = "" if name.startswith("CONFIG_") else known.get(name)
            if value is None:
                return None
            return bool(value) == (kind == "ifdef")
        m = re.match(r"^\((.*),(.*)\)$", args.strip())
        if not m:
            return None
        left, right = subst(m.group(1)), subst(m.group(2))
        if left is None or right is None:
            return None
        return (left.strip() == right.strip()) == (kind == "ifeq")

    stack: list[bool | None] = []
    result: str | None = None
    for raw in arch_makefile.splitlines():
        line = raw.strip()
        m = re.match(r"^(ifeq|ifneq|ifdef|ifndef)\s*(.*)$", line)
        if m:
            stack.append(condition(m.group(1), m.group(2)))
            continue
        if re.match(r"^else\b", line) and stack:
            stack[-1] = None if stack[-1] is None else not stack[-1]
            continue
        if re.match(r"^endif\b", line) and stack:
            stack.pop()
            continue
        if False in stack:
            continue
        a = re.match(r"^KBUILD_DEFCONFIG\s*:?=\s*(\S+)", line)
        if a:
            value = subst(a.group(1))
            if value:
                result = value
    return result


# Compact transport encoding (webapp): short keys, menus as arrays, file paths interned
_SYM_KEYS = (
    ("type", "t", None), ("prompts", "p", []), ("defaults", "d", []), ("ranges", "r", []),
    ("selects", "s", []), ("implies", "i", []), ("rev_dep", "rd", -1), ("implied", "im", -1),
    ("dir_dep", "dd", -1), ("menus", "m", []), ("choice", "ch", None), ("is_choice", "ic", False),
    ("optional", "op", False), ("members", "mb", []), ("auto", "au", False), ("env", "env", None),
    ("allnoconfig_y", "any", False),
)
MENU_FIELDS = ("parent", "kind", "sym", "prompt", "pvis", "vis_if", "dep", "file", "line_s", "line_e", "has_help")


def compact_model(model: dict[str, Any]) -> dict[str, Any]:
    """Shrink a model for transport (decoded by kconfig_engine.js decodeModel)."""
    files: list[str] = []
    file_index: dict[str, int] = {}

    def fidx(path: str) -> int:
        if path not in file_index:
            file_index[path] = len(files)
            files.append(path)
        return file_index[path]

    menus = [
        [n["parent_id"], n["node_type"], n["symbol_name"], n["prompt"], n["pvis"], n["vis_if"], n["dep"],
         fidx(n["file_path"]), n["line_s"], n["line_e"], 1 if n["has_help"] else 0]
        for n in model["nodes"]
    ]
    symbols = {}
    for name, sym in model["symbols"].items():
        out = {}
        for key, short, default in _SYM_KEYS:
            value = sym.get(key, default)
            if value != default:
                out[short] = value
        symbols[name] = out
    compact = {k: v for k, v in model.items() if k not in ("nodes", "symbols", "files")}
    compact.update({"menu_fields": MENU_FIELDS, "menus": menus, "symbols": symbols, "files": files, "kconfig_files": model["files"]})
    return compact
