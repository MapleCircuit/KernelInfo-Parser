"""parser/kconfig_ast/kconfig_lexer.py - High-Performance Lexer for Linux Kconfig files.

Tokenizes Kconfig syntax including identifiers, keywords, strings, operators,
line continuations, and indentation-sensitive multi-line help blocks.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from enum import Enum, auto


class TokenType(Enum):
    # Keywords
    MAINMENU = auto()
    MENU = auto()
    ENDMENU = auto()
    CONFIG = auto()
    MENUCONFIG = auto()
    CHOICE = auto()
    ENDCHOICE = auto()
    COMMENT = auto()
    IF = auto()
    ENDIF = auto()
    SOURCE = auto()
    RSOURCE = auto()

    # Types & Properties
    BOOL = auto()
    TRISTATE = auto()
    STRING = auto()
    HEX = auto()
    INT = auto()
    DEF_BOOL = auto()
    DEF_TRISTATE = auto()
    PROMPT = auto()
    DEFAULT = auto()
    DEPENDS = auto()
    ON = auto()
    SELECT = auto()
    IMPLY = auto()
    VISIBLE = auto()
    RANGE = auto()
    OPTION = auto()
    OPTIONAL = auto()
    HELP = auto()

    # Literals & Identifiers
    SYMBOL = auto()
    CONST_STRING = auto()
    NUMBER = auto()
    HELP_TEXT = auto()

    # Operators & Delimiters
    LPAREN = auto()
    RPAREN = auto()
    EXCLAMATION = auto()
    AMP_AMP = auto()
    PIPE_PIPE = auto()
    EQUAL = auto()
    NOT_EQUAL = auto()

    # Layout
    NEWLINE = auto()
    EOF = auto()


@dataclass
class Token:
    type: TokenType
    value: str
    line: int
    col: int
    end_line: int
    end_col: int


KEYWORDS = {
    "mainmenu": TokenType.MAINMENU,
    "menu": TokenType.MENU,
    "endmenu": TokenType.ENDMENU,
    "config": TokenType.CONFIG,
    "menuconfig": TokenType.MENUCONFIG,
    "choice": TokenType.CHOICE,
    "endchoice": TokenType.ENDCHOICE,
    "comment": TokenType.COMMENT,
    "if": TokenType.IF,
    "endif": TokenType.ENDIF,
    "source": TokenType.SOURCE,
    "rsource": TokenType.RSOURCE,
    "bool": TokenType.BOOL,
    "boolean": TokenType.BOOL,
    "tristate": TokenType.TRISTATE,
    "string": TokenType.STRING,
    "hex": TokenType.HEX,
    "int": TokenType.INT,
    "def_bool": TokenType.DEF_BOOL,
    "def_boolean": TokenType.DEF_BOOL,
    "def_tristate": TokenType.DEF_TRISTATE,
    "prompt": TokenType.PROMPT,
    "default": TokenType.DEFAULT,
    "depends": TokenType.DEPENDS,
    "on": TokenType.ON,
    "select": TokenType.SELECT,
    "imply": TokenType.IMPLY,
    "visible": TokenType.VISIBLE,
    "range": TokenType.RANGE,
    "option": TokenType.OPTION,
    "optional": TokenType.OPTIONAL,
    "help": TokenType.HELP,
    "---help---": TokenType.HELP,
}


class KconfigLexer:
    """Deterministic, whitespace-aware tokenizer for Linux Kconfig source."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.lines = text.splitlines(keepends=True)
        self.num_lines = len(self.lines)

    def tokenize(self) -> list[Token]:
        tokens: list[Token] = []
        line_idx = 0

        while line_idx < self.num_lines:
            raw_line = self.lines[line_idx]
            line_no = line_idx + 1

            # Handle line continuations with backslash '\'
            combined_line = raw_line
            end_line_no = line_no
            while combined_line.rstrip("\r\n").endswith("\\") and (line_idx + 1) < self.num_lines:
                line_idx += 1
                combined_line = combined_line.rstrip("\r\n")[:-1] + " " + self.lines[line_idx]
                end_line_no = line_idx + 1

            # Process line content
            line_tokens, in_help = self._tokenize_line(combined_line, line_no, end_line_no)
            tokens.extend(line_tokens)

            if in_help:
                # Capture subsequent indented help text
                help_text, help_start_line, help_end_line, next_line_idx = self._consume_help_block(line_idx + 1)
                if help_text is not None:
                    tokens.append(Token(
                        type=TokenType.HELP_TEXT,
                        value=help_text,
                        line=help_start_line,
                        col=1,
                        end_line=help_end_line,
                        end_col=len(self.lines[help_end_line - 1]) if help_end_line <= self.num_lines else 1,
                    ))
                line_idx = next_line_idx - 1

            line_idx += 1

        tokens.append(Token(TokenType.EOF, "", self.num_lines + 1, 1, self.num_lines + 1, 1))
        return tokens

    def _tokenize_line(self, line_text: str, line_no: int, end_line_no: int) -> tuple[list[Token], bool]:
        tokens: list[Token] = []
        idx = 0
        n = len(line_text)
        saw_help = False

        while idx < n:
            ch = line_text[idx]

            # Whitespace
            if ch in " \t\r":
                idx += 1
                continue

            # Newline
            if ch == "\n":
                tokens.append(Token(TokenType.NEWLINE, "\n", line_no, idx + 1, end_line_no, idx + 1))
                idx += 1
                break

            # Comment (stops rest of line)
            if ch == "#":
                tokens.append(Token(TokenType.NEWLINE, "\n", line_no, idx + 1, end_line_no, idx + 1))
                break

            # Strings
            if ch in ('"', "'"):
                quote_char = ch
                start_col = idx + 1
                idx += 1
                str_buf = []
                while idx < n and line_text[idx] != quote_char:
                    if line_text[idx] == "\\" and idx + 1 < n:
                        idx += 1
                        str_buf.append(line_text[idx])
                    else:
                        str_buf.append(line_text[idx])
                    idx += 1
                if idx < n and line_text[idx] == quote_char:
                    idx += 1
                tokens.append(Token(
                    type=TokenType.CONST_STRING,
                    value="".join(str_buf),
                    line=line_no,
                    col=start_col,
                    end_line=end_line_no,
                    end_col=idx,
                ))
                continue

            # Operators
            if line_text.startswith("&&", idx):
                tokens.append(Token(TokenType.AMP_AMP, "&&", line_no, idx + 1, end_line_no, idx + 2))
                idx += 2
                continue
            if line_text.startswith("||", idx):
                tokens.append(Token(TokenType.PIPE_PIPE, "||", line_no, idx + 1, end_line_no, idx + 2))
                idx += 2
                continue
            if line_text.startswith("!=", idx):
                tokens.append(Token(TokenType.NOT_EQUAL, "!=", line_no, idx + 1, end_line_no, idx + 2))
                idx += 2
                continue
            if ch == "=":
                tokens.append(Token(TokenType.EQUAL, "=", line_no, idx + 1, end_line_no, idx + 1))
                idx += 1
                continue
            if ch == "!":
                tokens.append(Token(TokenType.EXCLAMATION, "!", line_no, idx + 1, end_line_no, idx + 1))
                idx += 1
                continue
            if ch == "(":
                tokens.append(Token(TokenType.LPAREN, "(", line_no, idx + 1, end_line_no, idx + 1))
                idx += 1
                continue
            if ch == ")":
                tokens.append(Token(TokenType.RPAREN, ")", line_no, idx + 1, end_line_no, idx + 1))
                idx += 1
                continue

            # Identifiers, numbers, or keywords
            start_col = idx + 1
            # Special check for ---help---
            if line_text.startswith("---help---", idx):
                tokens.append(Token(TokenType.HELP, "---help---", line_no, start_col, end_line_no, idx + 10))
                idx += 10
                saw_help = True
                continue

            # Match symbol / keyword / number
            m = re.match(r"[A-Za-z0-9_\-\.\/]+", line_text[idx:])
            if m:
                val = m.group(0)
                idx += len(val)
                lower_val = val.lower()
                tok_type = KEYWORDS.get(lower_val, TokenType.SYMBOL)
                if tok_type == TokenType.HELP:
                    saw_help = True
                tokens.append(Token(tok_type, val, line_no, start_col, end_line_no, idx))
                continue

            # Any unknown character
            idx += 1

        return tokens, saw_help

    def _consume_help_block(self, start_idx: int) -> tuple[str | None, int, int, int]:
        """Consume multi-line help text block up to the first line with less indentation."""
        cur = start_idx
        while cur < self.num_lines and not self.lines[cur].strip():
            cur += 1

        if cur >= self.num_lines:
            return "", start_idx + 1, self.num_lines, cur

        first_line = self.lines[cur]
        # Check indentation of first help line
        indent_match = re.match(r"^([ \t]+)", first_line)
        if not indent_match:
            # Not indented -> no help body
            return "", start_idx + 1, start_idx + 1, cur

        initial_indent = indent_match.group(1)
        # Normalize tabs to 8 spaces for indentation comparison
        min_indent_len = len(initial_indent.expandtabs(8))

        help_lines = []
        block_start_line = cur + 1
        block_end_line = cur + 1

        while cur < self.num_lines:
            line = self.lines[cur]
            stripped = line.strip()
            if not stripped:
                help_lines.append("")
                block_end_line = cur + 1
                cur += 1
                continue

            m = re.match(r"^([ \t]+)", line)
            line_indent_len = len(m.group(1).expandtabs(8)) if m else 0
            if line_indent_len < min_indent_len:
                # End of help block
                break

            # Strip leading indent up to min_indent_len
            expanded = line.expandtabs(8)
            trimmed = expanded[min_indent_len:] if len(expanded) >= min_indent_len else expanded.lstrip()
            help_lines.append(trimmed.rstrip("\r\n"))
            block_end_line = cur + 1
            cur += 1

        return "\n".join(help_lines).strip(), block_start_line, block_end_line, cur
