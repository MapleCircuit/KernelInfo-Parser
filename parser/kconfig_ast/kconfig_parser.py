"""parser/kconfig_ast/kconfig_parser.py - Recursive Descent Parser for Linux Kconfig.

Parses token streams from KconfigLexer into strongly-typed AST nodes with
lexical extents, expression hierarchies, and block scoping.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any
from parser.kconfig_ast.kconfig_lexer import KconfigLexer, Token, TokenType


class ExprOp(Enum):
    SYMBOL_REF = auto()
    CONSTANT = auto()
    NOT = auto()
    AND = auto()
    OR = auto()
    EQUAL = auto()
    UNEQUAL = auto()


@dataclass
class KconfigExpr:
    op: ExprOp
    left: KconfigExpr | None = None
    right: KconfigExpr | None = None
    value: str | None = None
    line_s: int = 0
    char_s: int = 0
    line_e: int = 0
    char_e: int = 0

    def to_string(self) -> str:
        if self.op == ExprOp.SYMBOL_REF:
            return self.value or ""
        elif self.op == ExprOp.CONSTANT:
            return f'"{self.value}"'
        elif self.op == ExprOp.NOT:
            return f"!({self.left.to_string()})" if self.left else "!"
        elif self.op == ExprOp.AND:
            l_str = self.left.to_string() if self.left else ""
            r_str = self.right.to_string() if self.right else ""
            return f"({l_str} && {r_str})"
        elif self.op == ExprOp.OR:
            l_str = self.left.to_string() if self.left else ""
            r_str = self.right.to_string() if self.right else ""
            return f"({l_str} || {r_str})"
        elif self.op == ExprOp.EQUAL:
            l_str = self.left.to_string() if self.left else ""
            r_str = self.right.to_string() if self.right else ""
            return f"{l_str} = {r_str}"
        elif self.op == ExprOp.UNEQUAL:
            l_str = self.left.to_string() if self.left else ""
            r_str = self.right.to_string() if self.right else ""
            return f"{l_str} != {r_str}"
        return ""

    def collect_symbols(self) -> list[str]:
        syms: list[str] = []
        if self.op == ExprOp.SYMBOL_REF and self.value:
            syms.append(self.value)
        if self.left:
            syms.extend(self.left.collect_symbols())
        if self.right:
            syms.extend(self.right.collect_symbols())
        return syms


# Symbol Type Constants (mapped to DBLayout schema: 1: bool, 2: tristate, 3: string, 4: hex, 5: int)
TYPE_UNKNOWN = 0
TYPE_BOOL = 1
TYPE_TRISTATE = 2
TYPE_STRING = 3
TYPE_HEX = 4
TYPE_INT = 5


@dataclass
class KconfigConfig:
    name: str
    is_menuconfig: bool = False
    sym_type: int = TYPE_UNKNOWN
    prompt: str = ""
    prompt_cond: KconfigExpr | None = None
    defaults: list[tuple[KconfigExpr, KconfigExpr | None]] = field(default_factory=list)
    depends_on: list[KconfigExpr] = field(default_factory=list)
    selects: list[tuple[str, KconfigExpr | None]] = field(default_factory=list)
    implies: list[tuple[str, KconfigExpr | None]] = field(default_factory=list)
    ranges: list[tuple[str, str, KconfigExpr | None]] = field(default_factory=list)
    help_text: str = ""
    options: list[str] = field(default_factory=list)
    line_s: int = 0
    char_s: int = 0
    line_e: int = 0
    char_e: int = 0
    raw_code: str = ""


@dataclass
class KconfigMenu:
    title: str
    prompt_cond: KconfigExpr | None = None
    depends_on: list[KconfigExpr] = field(default_factory=list)
    visible_if: KconfigExpr | None = None
    children: list[Any] = field(default_factory=list)
    line_s: int = 0
    char_s: int = 0
    line_e: int = 0
    char_e: int = 0
    raw_code: str = ""


@dataclass
class KconfigChoice:
    name: str | None = None
    sym_type: int = TYPE_BOOL
    prompt: str = ""
    prompt_cond: KconfigExpr | None = None
    is_optional: bool = False
    defaults: list[tuple[str, KconfigExpr | None]] = field(default_factory=list)
    depends_on: list[KconfigExpr] = field(default_factory=list)
    children: list[Any] = field(default_factory=list)
    line_s: int = 0
    char_s: int = 0
    line_e: int = 0
    char_e: int = 0
    raw_code: str = ""


@dataclass
class KconfigIf:
    cond: KconfigExpr
    children: list[Any] = field(default_factory=list)
    line_s: int = 0
    char_s: int = 0
    line_e: int = 0
    char_e: int = 0
    raw_code: str = ""


@dataclass
class KconfigComment:
    title: str
    depends_on: list[KconfigExpr] = field(default_factory=list)
    line_s: int = 0
    char_s: int = 0
    line_e: int = 0
    char_e: int = 0
    raw_code: str = ""


@dataclass
class KconfigSource:
    path: str
    is_rsource: bool = False
    line_s: int = 0
    char_s: int = 0
    line_e: int = 0
    char_e: int = 0
    raw_code: str = ""


@dataclass
class KconfigMainmenu:
    title: str
    line_s: int = 0
    char_s: int = 0
    line_e: int = 0
    char_e: int = 0
    raw_code: str = ""


class KconfigParser:
    """Parses Kconfig token stream into structured syntax tree."""

    def __init__(self, tokens: list[Token], raw_text: str = "") -> None:
        self.tokens = tokens
        self.pos = 0
        self.raw_text = raw_text
        self.raw_lines = raw_text.splitlines(keepends=True) if raw_text else []

    def _current(self) -> Token:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return self.tokens[-1]

    def _peek(self, offset: int = 1) -> Token:
        idx = self.pos + offset
        if idx < len(self.tokens):
            return self.tokens[idx]
        return self.tokens[-1]

    def _advance(self) -> Token:
        tok = self._current()
        if self.pos < len(self.tokens):
            self.pos += 1
        return tok

    def _match(self, *types: TokenType) -> bool:
        if self._current().type in types:
            self._advance()
            return True
        return False

    def _expect(self, tok_type: TokenType) -> Token:
        cur = self._current()
        if cur.type == tok_type:
            return self._advance()
        # Non-fatal advance
        return self._advance()

    def _skip_newlines(self) -> None:
        while self._current().type == TokenType.NEWLINE:
            self._advance()

    def _extract_raw(self, line_s: int, line_e: int) -> str:
        if not self.raw_lines or line_s <= 0:
            return ""
        s_idx = max(0, line_s - 1)
        e_idx = min(len(self.raw_lines), line_e)
        return "".join(self.raw_lines[s_idx:e_idx])

    def parse(self) -> list[Any]:
        items: list[Any] = []
        self._skip_newlines()
        while self._current().type != TokenType.EOF:
            item = self._parse_top_level_item()
            if item is not None:
                items.append(item)
            self._skip_newlines()
        return items

    def _parse_top_level_item(self) -> Any | None:
        tok = self._current()

        if tok.type == TokenType.MAINMENU:
            return self._parse_mainmenu()
        elif tok.type in (TokenType.CONFIG, TokenType.MENUCONFIG):
            return self._parse_config(is_menuconfig=(tok.type == TokenType.MENUCONFIG))
        elif tok.type == TokenType.MENU:
            return self._parse_menu()
        elif tok.type == TokenType.CHOICE:
            return self._parse_choice()
        elif tok.type == TokenType.IF:
            return self._parse_if()
        elif tok.type == TokenType.COMMENT:
            return self._parse_comment()
        elif tok.type in (TokenType.SOURCE, TokenType.RSOURCE):
            return self._parse_source(is_rsource=(tok.type == TokenType.RSOURCE))
        else:
            # Skip unhandled token on line
            self._advance()
            while self._current().type not in (TokenType.NEWLINE, TokenType.EOF):
                self._advance()
            self._skip_newlines()
            return None

    def _parse_mainmenu(self) -> KconfigMainmenu:
        start_tok = self._advance()  # 'mainmenu'
        title = ""
        cur = self._current()
        if cur.type in (TokenType.CONST_STRING, TokenType.SYMBOL):
            title = cur.value
            self._advance()
        end_tok = cur
        self._skip_newlines()
        return KconfigMainmenu(
            title=title,
            line_s=start_tok.line,
            char_s=start_tok.col,
            line_e=end_tok.end_line,
            char_e=end_tok.end_col,
            raw_code=self._extract_raw(start_tok.line, end_tok.end_line),
        )

    def _parse_config(self, is_menuconfig: bool) -> KconfigConfig:
        start_tok = self._advance()  # 'config' or 'menuconfig'
        name_tok = self._current()
        sym_name = name_tok.value
        self._advance()
        self._skip_newlines()

        cfg = KconfigConfig(
            name=sym_name,
            is_menuconfig=is_menuconfig,
            line_s=start_tok.line,
            char_s=start_tok.col,
        )

        last_line = name_tok.end_line
        last_col = name_tok.end_col

        # Parse property lines until next major statement or EOF
        while self._current().type not in (
            TokenType.CONFIG,
            TokenType.MENUCONFIG,
            TokenType.MENU,
            TokenType.ENDMENU,
            TokenType.CHOICE,
            TokenType.ENDCHOICE,
            TokenType.IF,
            TokenType.ENDIF,
            TokenType.COMMENT,
            TokenType.SOURCE,
            TokenType.RSOURCE,
            TokenType.MAINMENU,
            TokenType.EOF,
        ):
            prop_tok = self._current()
            if prop_tok.type in (TokenType.BOOL, TokenType.TRISTATE, TokenType.STRING, TokenType.HEX, TokenType.INT):
                self._parse_type_definition(cfg)
            elif prop_tok.type in (TokenType.DEF_BOOL, TokenType.DEF_TRISTATE):
                self._parse_def_type_definition(cfg)
            elif prop_tok.type == TokenType.PROMPT:
                self._parse_prompt(cfg)
            elif prop_tok.type == TokenType.DEFAULT:
                self._parse_default(cfg)
            elif prop_tok.type == TokenType.DEPENDS:
                self._parse_depends_on(cfg.depends_on)
            elif prop_tok.type == TokenType.SELECT:
                self._parse_select(cfg.selects)
            elif prop_tok.type == TokenType.IMPLY:
                self._parse_select(cfg.implies)
            elif prop_tok.type == TokenType.RANGE:
                self._parse_range(cfg)
            elif prop_tok.type == TokenType.OPTION:
                self._parse_option(cfg)
            elif prop_tok.type == TokenType.HELP:
                self._parse_help(cfg)
            else:
                self._advance()

            last_line = self.tokens[self.pos - 1].end_line if self.pos > 0 else last_line
            last_col = self.tokens[self.pos - 1].end_col if self.pos > 0 else last_col
            self._skip_newlines()

        cfg.line_e = last_line
        cfg.char_e = last_col
        cfg.raw_code = self._extract_raw(cfg.line_s, cfg.line_e)
        return cfg

    def _parse_type_definition(self, cfg: KconfigConfig) -> None:
        type_tok = self._advance()
        type_map = {
            TokenType.BOOL: TYPE_BOOL,
            TokenType.TRISTATE: TYPE_TRISTATE,
            TokenType.STRING: TYPE_STRING,
            TokenType.HEX: TYPE_HEX,
            TokenType.INT: TYPE_INT,
        }
        cfg.sym_type = type_map.get(type_tok.type, TYPE_UNKNOWN)

        # Optional prompt on same line: bool "Prompt Text" if COND
        if self._current().type in (TokenType.CONST_STRING, TokenType.SYMBOL):
            cfg.prompt = self._advance().value
            if self._current().type == TokenType.IF:
                self._advance()
                cfg.prompt_cond = self._parse_expr()
        self._skip_newlines()

    def _parse_def_type_definition(self, cfg: KconfigConfig) -> None:
        type_tok = self._advance()
        cfg.sym_type = TYPE_BOOL if type_tok.type == TokenType.DEF_BOOL else TYPE_TRISTATE
        def_expr = self._parse_expr()
        cond_expr = None
        if self._current().type == TokenType.IF:
            self._advance()
            cond_expr = self._parse_expr()
        if def_expr:
            cfg.defaults.append((def_expr, cond_expr))
        self._skip_newlines()

    def _parse_prompt(self, cfg: KconfigConfig) -> None:
        self._advance()  # 'prompt'
        if self._current().type in (TokenType.CONST_STRING, TokenType.SYMBOL):
            cfg.prompt = self._advance().value
            if self._current().type == TokenType.IF:
                self._advance()
                cfg.prompt_cond = self._parse_expr()
        self._skip_newlines()

    def _parse_default(self, cfg: KconfigConfig) -> None:
        self._advance()  # 'default'
        def_expr = self._parse_expr()
        cond_expr = None
        if self._current().type == TokenType.IF:
            self._advance()
            cond_expr = self._parse_expr()
        if def_expr:
            cfg.defaults.append((def_expr, cond_expr))
        self._skip_newlines()

    def _parse_depends_on(self, target_list: list[KconfigExpr]) -> None:
        self._advance()  # 'depends'
        if self._current().type == TokenType.ON:
            self._advance()  # 'on'
        expr = self._parse_expr()
        if expr:
            target_list.append(expr)
        self._skip_newlines()

    def _parse_select(self, target_list: list[tuple[str, KconfigExpr | None]]) -> None:
        self._advance()  # 'select' or 'imply'
        if self._current().type in (TokenType.SYMBOL, TokenType.CONST_STRING):
            target_sym = self._advance().value
            cond_expr = None
            if self._current().type == TokenType.IF:
                self._advance()
                cond_expr = self._parse_expr()
            target_list.append((target_sym, cond_expr))
        self._skip_newlines()

    def _parse_range(self, cfg: KconfigConfig) -> None:
        self._advance()  # 'range'
        min_val = self._advance().value if self._current().type in (TokenType.SYMBOL, TokenType.NUMBER, TokenType.CONST_STRING) else ""
        max_val = self._advance().value if self._current().type in (TokenType.SYMBOL, TokenType.NUMBER, TokenType.CONST_STRING) else ""
        cond = None
        if self._current().type == TokenType.IF:
            self._advance()
            cond = self._parse_expr()
        if min_val and max_val:
            cfg.ranges.append((min_val, max_val, cond))
        self._skip_newlines()

    def _parse_option(self, cfg: KconfigConfig) -> None:
        self._advance()  # 'option'
        opt_str = []
        while self._current().type not in (TokenType.NEWLINE, TokenType.EOF):
            opt_str.append(self._advance().value)
        cfg.options.append(" ".join(opt_str))
        self._skip_newlines()

    def _parse_help(self, cfg: KconfigConfig) -> None:
        self._advance()  # 'help' or '---help---'
        self._skip_newlines()
        if self._current().type == TokenType.HELP_TEXT:
            cfg.help_text = self._advance().value
        self._skip_newlines()

    def _parse_menu(self) -> KconfigMenu:
        start_tok = self._advance()  # 'menu'
        title = ""
        if self._current().type in (TokenType.CONST_STRING, TokenType.SYMBOL):
            title = self._advance().value
        self._skip_newlines()

        menu = KconfigMenu(
            title=title,
            line_s=start_tok.line,
            char_s=start_tok.col,
        )

        # Parse properties and sub-blocks until endmenu or EOF
        while self._current().type not in (TokenType.ENDMENU, TokenType.EOF):
            cur = self._current()
            if cur.type == TokenType.DEPENDS:
                self._parse_depends_on(menu.depends_on)
            elif cur.type == TokenType.VISIBLE:
                self._advance()
                if self._current().type == TokenType.IF:
                    self._advance()
                    menu.visible_if = self._parse_expr()
                self._skip_newlines()
            else:
                item = self._parse_top_level_item()
                if item:
                    menu.children.append(item)
            self._skip_newlines()

        end_tok = self._current()
        if self._current().type == TokenType.ENDMENU:
            end_tok = self._advance()
        self._skip_newlines()

        menu.line_e = end_tok.end_line
        menu.char_e = end_tok.end_col
        menu.raw_code = self._extract_raw(menu.line_s, menu.line_e)
        return menu

    def _parse_choice(self) -> KconfigChoice:
        start_tok = self._advance()  # 'choice'
        name = None
        if self._current().type == TokenType.SYMBOL:
            name = self._advance().value
        self._skip_newlines()

        choice = KconfigChoice(
            name=name,
            line_s=start_tok.line,
            char_s=start_tok.col,
        )

        while self._current().type not in (TokenType.ENDCHOICE, TokenType.EOF):
            cur = self._current()
            if cur.type in (TokenType.BOOL, TokenType.TRISTATE):
                choice.sym_type = TYPE_BOOL if cur.type == TokenType.BOOL else TYPE_TRISTATE
                self._advance()
                if self._current().type in (TokenType.CONST_STRING, TokenType.SYMBOL):
                    choice.prompt = self._advance().value
                self._skip_newlines()
            elif cur.type == TokenType.PROMPT:
                self._advance()
                if self._current().type in (TokenType.CONST_STRING, TokenType.SYMBOL):
                    choice.prompt = self._advance().value
                self._skip_newlines()
            elif cur.type == TokenType.OPTIONAL:
                self._advance()
                choice.is_optional = True
                self._skip_newlines()
            elif cur.type == TokenType.DEFAULT:
                self._advance()
                def_sym = self._advance().value if self._current().type in (TokenType.SYMBOL, TokenType.CONST_STRING) else ""
                cond = None
                if self._current().type == TokenType.IF:
                    self._advance()
                    cond = self._parse_expr()
                if def_sym:
                    choice.defaults.append((def_sym, cond))
                self._skip_newlines()
            elif cur.type == TokenType.DEPENDS:
                self._parse_depends_on(choice.depends_on)
            else:
                item = self._parse_top_level_item()
                if item:
                    choice.children.append(item)
            self._skip_newlines()

        end_tok = self._current()
        if self._current().type == TokenType.ENDCHOICE:
            end_tok = self._advance()
        self._skip_newlines()

        choice.line_e = end_tok.end_line
        choice.char_e = end_tok.end_col
        choice.raw_code = self._extract_raw(choice.line_s, choice.line_e)
        return choice

    def _parse_if(self) -> KconfigIf:
        start_tok = self._advance()  # 'if'
        cond_expr = self._parse_expr() or KconfigExpr(op=ExprOp.SYMBOL_REF, value="y")
        self._skip_newlines()

        if_node = KconfigIf(
            cond=cond_expr,
            line_s=start_tok.line,
            char_s=start_tok.col,
        )

        while self._current().type not in (TokenType.ENDIF, TokenType.EOF):
            item = self._parse_top_level_item()
            if item:
                if_node.children.append(item)
            self._skip_newlines()

        end_tok = self._current()
        if self._current().type == TokenType.ENDIF:
            end_tok = self._advance()
        self._skip_newlines()

        if_node.line_e = end_tok.end_line
        if_node.char_e = end_tok.end_col
        if_node.raw_code = self._extract_raw(if_node.line_s, if_node.line_e)
        return if_node

    def _parse_comment(self) -> KconfigComment:
        start_tok = self._advance()  # 'comment'
        title = ""
        if self._current().type in (TokenType.CONST_STRING, TokenType.SYMBOL):
            title = self._advance().value
        self._skip_newlines()

        comment = KconfigComment(
            title=title,
            line_s=start_tok.line,
            char_s=start_tok.col,
        )

        while self._current().type == TokenType.DEPENDS:
            self._parse_depends_on(comment.depends_on)
            self._skip_newlines()

        last_tok = self.tokens[self.pos - 1] if self.pos > 0 else start_tok
        comment.line_e = last_tok.end_line
        comment.char_e = last_tok.end_col
        comment.raw_code = self._extract_raw(comment.line_s, comment.line_e)
        return comment

    def _parse_source(self, is_rsource: bool) -> KconfigSource:
        start_tok = self._advance()  # 'source' or 'rsource'
        path_str = ""
        if self._current().type in (TokenType.CONST_STRING, TokenType.SYMBOL):
            path_str = self._advance().value
        self._skip_newlines()

        return KconfigSource(
            path=path_str,
            is_rsource=is_rsource,
            line_s=start_tok.line,
            char_s=start_tok.col,
            line_e=start_tok.end_line,
            char_e=start_tok.end_col,
            raw_code=self._extract_raw(start_tok.line, start_tok.end_line),
        )

    # -------------------------------------------------------------------------
    # Expression Parsing with Precedence Climbing:
    # 1. ( expr )
    # 2. =, !=
    # 3. !
    # 4. &&
    # 5. ||
    # -------------------------------------------------------------------------
    def _parse_expr(self) -> KconfigExpr | None:
        return self._parse_or_expr()

    def _parse_or_expr(self) -> KconfigExpr | None:
        left = self._parse_and_expr()
        while self._current().type == TokenType.PIPE_PIPE:
            op_tok = self._advance()
            right = self._parse_and_expr()
            if left and right:
                left = KconfigExpr(
                    op=ExprOp.OR,
                    left=left,
                    right=right,
                    line_s=left.line_s,
                    char_s=left.char_s,
                    line_e=right.line_e,
                    char_e=right.char_e,
                )
        return left

    def _parse_and_expr(self) -> KconfigExpr | None:
        left = self._parse_not_expr()
        while self._current().type == TokenType.AMP_AMP:
            op_tok = self._advance()
            right = self._parse_not_expr()
            if left and right:
                left = KconfigExpr(
                    op=ExprOp.AND,
                    left=left,
                    right=right,
                    line_s=left.line_s,
                    char_s=left.char_s,
                    line_e=right.line_e,
                    char_e=right.char_e,
                )
        return left

    def _parse_not_expr(self) -> KconfigExpr | None:
        if self._current().type == TokenType.EXCLAMATION:
            op_tok = self._advance()
            sub = self._parse_not_expr()
            if sub:
                return KconfigExpr(
                    op=ExprOp.NOT,
                    left=sub,
                    line_s=op_tok.line,
                    char_s=op_tok.col,
                    line_e=sub.line_e,
                    char_e=sub.char_e,
                )
        return self._parse_relational_expr()

    def _parse_relational_expr(self) -> KconfigExpr | None:
        left = self._parse_primary_expr()
        if self._current().type in (TokenType.EQUAL, TokenType.NOT_EQUAL):
            op_tok = self._advance()
            right = self._parse_primary_expr()
            op = ExprOp.EQUAL if op_tok.type == TokenType.EQUAL else ExprOp.UNEQUAL
            if left and right:
                return KconfigExpr(
                    op=op,
                    left=left,
                    right=right,
                    line_s=left.line_s,
                    char_s=left.char_s,
                    line_e=right.line_e,
                    char_e=right.char_e,
                )
        return left

    def _parse_primary_expr(self) -> KconfigExpr | None:
        cur = self._current()
        if cur.type == TokenType.LPAREN:
            self._advance()
            expr = self._parse_expr()
            self._match(TokenType.RPAREN)
            return expr
        elif cur.type == TokenType.CONST_STRING:
            tok = self._advance()
            return KconfigExpr(
                op=ExprOp.CONSTANT,
                value=tok.value,
                line_s=tok.line,
                char_s=tok.col,
                line_e=tok.end_line,
                char_e=tok.end_col,
            )
        elif cur.type in (TokenType.SYMBOL, TokenType.NUMBER, TokenType.BOOL, TokenType.TRISTATE):
            tok = self._advance()
            return KconfigExpr(
                op=ExprOp.SYMBOL_REF,
                value=tok.value,
                line_s=tok.line,
                char_s=tok.col,
                line_e=tok.end_line,
                char_e=tok.end_col,
            )
        return None
