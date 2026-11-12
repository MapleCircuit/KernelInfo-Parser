"""parser/man_ast/man_parser.py - roff `man` page (plus a basic `mdoc` subset) -> JSON document model (pure).

`parse(text)` returns the format-1 document model specified in `parser/man_ast/MAN_API.md` §3; it never
raises. The interpreter is a single forward pass over logical input lines (comments stripped, `\\<newline>`
continuations joined) with a small roff layer (`.de` user macros, `.ds` strings, `.if/.ie/.el` conditions
evaluated as nroff, `.ig`) feeding a `man` block builder (sections, paragraphs, tagged/indented paragraphs,
relative indents, no-fill/example blocks, synopsis macros, links, tbl tables) and an inline escape scanner.
"""
from __future__ import annotations

import re
import unicodedata
from collections import deque
from typing import Any

FORMAT = 1
_RAISE = False  # test/smoke hook: re-raise internal errors instead of returning the fallback document

MAX_EXPANSION = 200_000  # total lines user-macro expansion may add to the input
MAX_MACRO_DEPTH = 32
MAX_STRING_DEPTH = 8

# --------------------------------------------------------------------------------------------------------------
# Character tables
# --------------------------------------------------------------------------------------------------------------
NAMED_CHARS: dict[str, str] = {
    "em": "\u2014", "en": "\u2013", "hy": "-", "mi": "-", "bu": "\u2022", "aq": "'", "dq": '"',
    "lq": "\u201c", "rq": "\u201d", "oq": "\u2018", "cq": "\u2019", "Bq": "\u201e", "bq": "\u201a",
    "Fo": "\u00ab", "Fc": "\u00bb", "fo": "\u2039", "fc": "\u203a",
    "co": "\u00a9", "rg": "\u00ae", "tm": "\u2122", "mu": "\u00d7", "di": "\u00f7", "pl": "+", "eq": "=",
    "ti": "~", "ha": "^", "ga": "`", "aa": "\u00b4", "ua": "\u2191", "da": "\u2193", "ra": "\u27e9",
    "la": "\u27e8", "->": "\u2192", "<-": "\u2190", "<>": "\u2194", "rA": "\u21d2", "lA": "\u21d0",
    "hA": "\u21d4", "uA": "\u21d1", "dA": "\u21d3", "<=": "\u2264", ">=": "\u2265", "!=": "\u2260",
    "==": "\u2261", "~=": "\u2245", "~~": "\u2248", "ap": "\u223c", "+-": "\u00b1", "-+": "\u2213",
    "dg": "\u2020", "dd": "\u2021", "sc": "\u00a7", "ps": "\u00b6", "de": "\u00b0", "fm": "\u2032",
    "sd": "\u2033", "rs": "\\", "sl": "/", "ba": "|", "br": "\u2502", "bv": "\u2502", "or": "|",
    "ul": "_", "rn": "\u203e", "at": "@", "sh": "#", "Do": "$", "Eu": "\u20ac", "eu": "\u20ac",
    "ct": "\u00a2", "Po": "\u00a3", "Ye": "\u00a5", "pc": "\u00b7", "**": "*", "lB": "[", "rB": "]",
    "lC": "{", "rC": "}", "if": "\u221e", "es": "\u2205", "mo": "\u2208", "nm": "\u2209", "sr": "\u221a",
    "pt": "\u221d", "is": "\u222b", "pd": "\u2202", "gr": "\u2207", "no": "\u00ac", "AN": "\u2227",
    "OR": "\u2228", "fa": "\u2200", "te": "\u2203", "ca": "\u2229", "cu": "\u222a", "sb": "\u2282",
    "sp": "\u2283", "ib": "\u2286", "ip": "\u2287", "sq": "\u25a1", "ci": "\u25cb", "OK": "\u2713",
    "12": "\u00bd", "14": "\u00bc", "34": "\u00be", "S1": "\u00b9", "S2": "\u00b2", "S3": "\u00b3",
    "ss": "\u00df", "AE": "\u00c6", "ae": "\u00e6", "OE": "\u0152", "oe": "\u0153", "/O": "\u00d8",
    "/o": "\u00f8", "oA": "\u00c5", "oa": "\u00e5", "TP": "\u00de", "Tp": "\u00fe", "-D": "\u00d0",
    "Sd": "\u00f0", "r!": "\u00a1", "r?": "\u00bf", "ff": "ff", "fi": "fi", "fl": "fl", "Fi": "ffi",
    "Fl": "ffl", "ts": "\u03c2", "u8": "", "radicalex": "",
    "lh": "\u261c", "rh": "\u261e", "an": "\u23af", "tno": "\u00ac", "ho": "\u02db", "a^": "^",
    "a~": "~", "a-": "\u00af", "ab": "\u02d8", "a.": "\u02d9", "a\"": "\u02dd", "ac": "\u00b8",
    "ad": "\u00a8", "ao": "\u02da", "ah": "\u02c7", "Sl": "\u2215", "mc": "\u00b5", "shc": "",
}
_GREEK_SRC = "abgdezyhiklmncoprstufxqw"
_GREEK_DST = "\u03b1\u03b2\u03b3\u03b4\u03b5\u03b6\u03b7\u03b8\u03b9\u03ba\u03bb\u03bc\u03bd\u03be\u03bf" \
             "\u03c0\u03c1\u03c3\u03c4\u03c5\u03c6\u03c7\u03c8\u03c9"
for _s, _d in zip(_GREEK_SRC, _GREEK_DST):
    NAMED_CHARS["*" + _s] = _d
    NAMED_CHARS["*" + _s.upper()] = _d.upper()
_ACCENTS = {"'": "\u0301", "`": "\u0300", ":": "\u0308", "^": "\u0302", "~": "\u0303", ",": "\u0327",
            "v": "\u030c", "o": "\u030a", "u": "\u0306", "-": "\u0304", ".": "\u0307", "h": "\u030b"}

# Predefined strings (man + mdoc)
DEFAULT_STRINGS: dict[str, str] = {
    "R": "\u00ae", "S": "", "Tm": "\u2122", "lq": "\u201c", "rq": "\u201d", "HF": "", "Lq": "\u201c",
    "Rq": "\u201d", "Ba": "|", "Ne": "\u2260", "Ge": "\u2265", "Le": "\u2264", "Gt": ">", "Lt": "<",
    "Pm": "\u00b1", "If": "\u221e", "Pi": "\u03c0", "Na": "NaN", "Am": "&", "Br": "[", "Ket": "]",
    "q": '"', "Rx": "", "en": "\u2013", "em": "\u2014", "MF": "I", "Aq": "", "Ef": "",
}
REGISTERS = {".g": 1, ".x": 1, ".y": 23, ".T": 0, ".$": 0}

FONTS = {
    "R": "R", "1": "R", "I": "I", "2": "I", "B": "B", "3": "B", "BI": "BI", "4": "BI",
    "CW": "CR", "CR": "CR", "C": "CR", "CB": "B", "CI": "I", "CBI": "BI", "TT": "CR", "L": "CR",
    "TR": "R", "TB": "B", "TI": "I", "TBI": "BI", "HR": "R", "H": "R", "HB": "B", "HI": "I", "HBI": "BI",
    "CO": "CR", "CX": "CR", "NR": "R", "NB": "B", "NI": "I", "PR": "R", "PB": "B", "PI": "I",
}

REF_NAME_RE = re.compile(r"^[A-Za-z_][\w.+:\-]*$")
REF_SEC_RE = re.compile(r"^\((\d[A-Za-z]*)\)")
# escapes that take a delimited argument: \w'..' \h'..' etc.
_DELIM_ESC = set("whvlLDxbobXZNABRSHTC")
# escapes taking a name argument (1 char, (xx or [xxx])
_NAME_ESC = set("FmMgkVY$O")

_BUILTINS = {
    "TH", "SH", "SS", "TP", "TQ", "IP", "HP", "P", "PP", "LP", "RS", "RE", "nf", "fi", "EX", "EE", "br",
    "sp", "B", "I", "SM", "SB", "BR", "BI", "IB", "IR", "RB", "RI", "UR", "UE", "MT", "ME", "MR", "SY",
    "OP", "YS", "TS", "ft", "so", "de", "de1", "am", "am1", "ds", "ds1", "as", "as1", "ig", "if", "ie",
    "el", "rm", "rn",
}


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


# --------------------------------------------------------------------------------------------------------------
# Input preprocessing
# --------------------------------------------------------------------------------------------------------------
def _strip_comment(line: str) -> tuple[str, bool, bool]:
    """(line without `\\"`/`\\#` comment, joins-next-line, had-comment)."""
    i = 0
    n = len(line)
    while True:
        j = line.find("\\", i)
        if j < 0 or j + 1 >= n:
            return line, False, False
        c = line[j + 1]
        if c == '"':
            return line[:j], False, True
        if c == "#":
            return line[:j], True, True
        i = j + 2


def _logical_lines(text: str) -> list[str]:
    phys = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    buf = ""
    joining = False
    for raw in phys:
        line, join_next, had_comment = _strip_comment(raw)
        if had_comment:
            line = line.rstrip()
        if not join_next:
            k = len(line) - len(line.rstrip("\\"))
            if k % 2 == 1:
                line = line[:-1]
                join_next = True
        if joining:
            buf += line
        else:
            if had_comment and not line.strip() and not join_next:
                continue
            buf = line
        if join_next:
            joining = True
            continue
        joining = False
        if had_comment and not buf.strip():
            continue
        out.append(buf)
    if joining and buf.strip():
        out.append(buf)
    return out


def _split_request(line: str) -> tuple[str, str]:
    body = line[1:].lstrip(" \t")
    if not body:
        return "", ""
    if body.startswith("\\"):
        m = re.match(r"(\S+)\s*(.*)$", body, re.DOTALL)
    else:
        m = re.match(r"([^\s\\]+)[ \t]*(.*)$", body, re.DOTALL)
    if not m:
        return "", ""
    return m.group(1), m.group(2)


def parse_args(s: str) -> list[str]:
    """roff macro arguments: blank-separated, double-quoted args may hold blanks (`""` is a literal quote)."""
    args: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        while i < n and s[i] in " \t":
            i += 1
        if i >= n:
            break
        if s[i] == '"':
            i += 1
            buf: list[str] = []
            while i < n:
                c = s[i]
                if c == '"':
                    if i + 1 < n and s[i + 1] == '"':
                        buf.append('"')
                        i += 2
                        continue
                    i += 1
                    break
                if c == "\\" and i + 1 < n:
                    buf.append(s[i:i + 2])
                    i += 2
                    continue
                buf.append(c)
                i += 1
            args.append("".join(buf))
        else:
            j = i
            while i < n and s[i] not in " \t":
                i += 2 if (s[i] == "\\" and i + 1 < n) else 1
            args.append(s[j:i])
    return args


def _copy_mode(line: str) -> str:
    """Definition-time copy mode: `\\\\` -> `\\` (other escapes kept for interpretation at use)."""
    if "\\\\" not in line:
        return line
    out = []
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c == "\\" and i + 1 < n:
            out.append("\\" if line[i + 1] == "\\" else line[i:i + 2])
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


_MACRO_ARG_RE = re.compile(r"\\\$(\d|\(\d\d|\[\d+\]|\*|@|0)|\\n(?:\(\.\$|\[\.\$\])")


def _named_char(name: str) -> str:
    if name in NAMED_CHARS:
        return NAMED_CHARS[name]
    if name.startswith("u") and len(name) > 1:
        parts = name[1:].split("_")
        try:
            return unicodedata.normalize("NFC", "".join(chr(int(p, 16)) for p in parts))
        except (ValueError, OverflowError):
            pass
    if name.startswith("char") and name[4:].isdigit():
        try:
            return chr(int(name[4:]))
        except (ValueError, OverflowError):
            return ""
    if len(name) == 2 and name[0] in _ACCENTS and name[1].isalpha():
        composed = unicodedata.normalize("NFC", name[1] + _ACCENTS[name[0]])
        if len(composed) == 1:
            return composed
    if len(name) == 1:
        return name
    return ""


def plain_text(runs: list[dict[str, Any]]) -> str:
    """Text of an inline run list (`{"br":1}` -> newline)."""
    return "".join("\n" if "br" in r else r.get("x", "") for r in runs)


# --------------------------------------------------------------------------------------------------------------
# Interpreter
# --------------------------------------------------------------------------------------------------------------
class _Parser:
    def __init__(self, text: str, strings: dict[str, str] | None = None,
                 macros: dict[str, list[str]] | None = None) -> None:
        self.queue: deque[tuple[str, int]] = deque((ln, 0) for ln in _logical_lines(text))
        self.budget = MAX_EXPANSION
        self.depth = 0  # macro-expansion depth of the line being processed
        self.strings: dict[str, str] = dict(DEFAULT_STRINGS) if strings is None else strings
        self.macros: dict[str, list[str]] = {} if macros is None else macros
        self.th = {"title": "", "section": "", "date": "", "source": "", "manual": ""}
        self.th_seen = False
        self.sections: list[dict[str, Any]] = []
        self.sec: dict[str, Any] | None = None
        self.stack: list[list[dict[str, Any]]] = []
        self.item: dict[str, Any] | None = None
        self.para: dict[str, Any] | None = None
        self.pre: dict[str, Any] | None = None
        self.nofill = False
        self.ex = False
        self.fs = ["R", "R"]  # current font, previous font
        self.pending_tag: dict[str, Any] | None = None
        self.pending_heading: dict[str, Any] | None = None
        self.next_font: str | None = None
        self.cont = False
        self.sy = False
        self.sy_line = False
        self.link: str | None = None
        self.link_mark = 0
        self.emitted = 0
        self.ie_stack: list[bool] = []
        # mdoc state
        self.mdoc_name = ""
        self.mdoc_lists: list[dict[str, Any]] = []
        self.mdoc_bd: list[tuple[bool, bool]] = []
        self.mdoc_last = ""

    # ---------------------------------------------------------------- containers
    def _container(self) -> list[dict[str, Any]]:
        if self.sec is None:
            self.sec = {"id": "", "title": "", "blocks": []}
            self.sections.append(self.sec)
            self.stack = [self.sec["blocks"]]
        return self.stack[-1]

    def _target(self) -> list[dict[str, Any]]:
        if self.item is not None:
            return self.item["body"]
        return self._container()

    def _reset_flow(self) -> None:
        self.para = None
        self.pre = None
        self.cont = False
        self.sy_line = False

    def _close_item(self) -> None:
        self.item = None
        self.pending_tag = None
        self._reset_flow()

    def _new_section(self, title: str) -> None:
        self._close_item()
        self.sec = {"id": "", "title": title, "blocks": []}
        self.sections.append(self.sec)
        self.stack = [self.sec["blocks"]]
        self.nofill = False
        self.ex = False
        self.fs = ["R", "R"]
        self.sy = False
        self.link = None
        self.mdoc_lists = []
        self.mdoc_bd = []

    # ---------------------------------------------------------------- inline scanning
    def _name_arg(self, s: str, i: int) -> tuple[str, int]:
        n = len(s)
        if i >= n:
            return "", i
        if s[i] == "(":
            return s[i + 1:i + 3], min(n, i + 3)
        if s[i] == "[":
            j = s.find("]", i + 1)
            if j < 0:
                return s[i + 1:], n
            return s[i + 1:j], j + 1
        return s[i], i + 1

    @staticmethod
    def _delim_arg(s: str, i: int) -> tuple[str, int]:
        n = len(s)
        if i >= n:
            return "", i
        d = s[i]
        j = s.find(d, i + 1)
        if j < 0:
            return s[i + 1:], n
        return s[i + 1:j], j + 1

    def _scan(self, s: str, depth: int = 0) -> list[tuple[str, str]]:
        """Tokens: ("t", text), ("f", font name), ("c", "") continuation."""
        out: list[tuple[str, str]] = []
        buf: list[str] = []
        i = 0
        n = len(s)

        def flush() -> None:
            if buf:
                out.append(("t", "".join(buf)))
                buf.clear()

        while i < n:
            c = s[i]
            if c != "\\":
                buf.append(c)
                i += 1
                continue
            if i + 1 >= n:
                break
            e = s[i + 1]
            i += 2
            if e in "\\eE":
                buf.append("\\")
            elif e == "-":
                buf.append("-")
            elif e in "&)%:|^,/{}!pdurazq?":
                pass  # zero-width / motion / hyphenation escapes (\z keeps the following character)
            elif e in "~ 0":
                buf.append("\u00a0")
            elif e == "t":
                buf.append("\t")
            elif e == "'":
                buf.append("'")
            elif e == "`":
                buf.append("`")
            elif e == ".":
                buf.append(".")
            elif e == "c":
                flush()
                out.append(("c", ""))
                return out
            elif e == "(":
                name = s[i:i + 2]
                i = min(n, i + 2)
                buf.append(_named_char(name))
            elif e == "[":
                j = s.find("]", i)
                name = s[i:] if j < 0 else s[i:j]
                i = n if j < 0 else j + 1
                buf.append(_named_char(name.split(" ")[0]))
            elif e == "*":
                name, i = self._name_arg(s, i)
                name = name.split(" ")[0]
                val = self.strings.get(name, "")
                if val and depth < MAX_STRING_DEPTH:
                    flush()
                    out.extend(self._scan(val, depth + 1))
            elif e == "f":
                name, i = self._name_arg(s, i)
                flush()
                out.append(("f", name))
            elif e == "n":
                if i < n and s[i] in "+-":
                    i += 1
                name, i = self._name_arg(s, i)
                if name in REGISTERS:
                    buf.append(str(REGISTERS[name]))
            elif e == "s":
                if i < n and s[i] in "+-":
                    i += 1
                if i < n and s[i] in "(['":
                    if s[i] == "(":
                        i = min(n, i + 3)
                    elif s[i] == "[":
                        j = s.find("]", i)
                        i = n if j < 0 else j + 1
                    else:
                        _, i = self._delim_arg(s, i)
                elif i < n and s[i].isdigit():
                    if s[i] in "123" and i + 1 < n and s[i + 1].isdigit():
                        i += 2
                    else:
                        i += 1
            elif e in _NAME_ESC:
                _, i = self._name_arg(s, i)
            elif e in _DELIM_ESC:
                arg, i = self._delim_arg(s, i)
                if e == "N":
                    try:
                        buf.append(chr(int(arg)))
                    except (ValueError, OverflowError):
                        pass
                elif e == "C":
                    buf.append(_named_char(arg))
                elif e == "Z" and depth < MAX_STRING_DEPTH:
                    flush()
                    out.extend(t for t in self._scan(arg, depth + 1) if t[0] != "c")
            elif e in '"#':
                break
            else:
                buf.append(e)
        flush()
        return out

    def _mk(self, text: str, font: str) -> dict[str, Any]:
        r: dict[str, Any] = {"x": text}
        if font and font != "R":
            r["f"] = font
        if self.link:
            r["url"] = self.link
        return r

    def _runs(self, s: str, fs: list[str]) -> tuple[list[dict[str, Any]], bool]:
        runs: list[dict[str, Any]] = []
        cont = False
        for kind, val in self._scan(s):
            if kind == "t":
                if val:
                    _append(runs, self._mk(val, fs[0]))
            elif kind == "f":
                if val in ("P", "", "]"):
                    fs[0], fs[1] = fs[1], fs[0]
                else:
                    new = FONTS.get(val, FONTS.get(val.upper(), "R"))
                    fs[1] = fs[0]
                    fs[0] = new
            elif kind == "c":
                cont = True
        return runs, cont

    def _plain(self, s: str) -> str:
        runs, _ = self._runs(s, ["R", "R"])
        return plain_text(runs)

    # ---------------------------------------------------------------- emission
    @staticmethod
    def _sep(prev: list[dict[str, Any]], nxt: list[dict[str, Any]]) -> dict[str, Any]:
        """Inter-line space run: part of a link only when both neighbours are in that link."""
        r: dict[str, Any] = {"x": " "}
        url = prev[-1].get("url") if prev else None
        if url and nxt and nxt[0].get("url") == url:
            r["url"] = url
        return r

    def _emit(self, runs: list[dict[str, Any]], cont: bool = False, nospace: bool = False) -> None:
        if not runs:
            if cont:
                self.cont = True
            return
        self.emitted += 1
        joined = self.cont or nospace
        if self.pending_heading is not None:
            h = self.pending_heading
            h["title"] = (h["title"] + ("" if joined or not h["title"] else " ") + plain_text(runs)).strip()
            if not cont:
                self.pending_heading = None
            self.cont = cont
            return
        if self.pending_tag is not None:
            tag = self.pending_tag["tags"][-1]
            if tag and not joined:
                tag.append(self._sep(tag, runs))
            tag.extend(runs)
            if not cont:
                self.pending_tag = None
            self.cont = cont
            return
        if self.nofill:
            if self.pre is None:
                self.pre = {"t": "pre", "ex": self.ex, "lines": []}
                self._target().append(self.pre)
            lines = self.pre["lines"]
            if lines and (joined or (self.sy and self.sy_line)):
                if not joined and lines[-1]:
                    lines[-1].append(self._sep(lines[-1], runs))
                lines[-1].extend(runs)
            else:
                lines.append(list(runs))
            if self.sy:
                self.sy_line = True
        else:
            if self.para is None:
                self.para = {"t": "p", "c": []}
                self._target().append(self.para)
            c = self.para["c"]
            if c and not joined and "br" not in c[-1]:
                c.append(self._sep(c, runs))
            c.extend(runs)
        self.cont = cont

    def _blank_line(self) -> None:
        if self.nofill:
            if self.pending_tag is not None or self.pending_heading is not None:
                return
            if self.pre is None:
                self.pre = {"t": "pre", "ex": self.ex, "lines": []}
                self._target().append(self.pre)
            self.pre["lines"].append([])
            self.cont = False
        else:
            self.para = None
            self.cont = False

    def _break(self) -> None:
        if self.nofill:
            self.cont = False
            return
        if self.para is not None and self.para["c"] and "br" not in self.para["c"][-1]:
            self.para["c"].append({"br": 1})
        self.cont = False

    # ---------------------------------------------------------------- main loop
    def run(self) -> dict[str, Any]:
        self.run_lines()
        return self._finish()

    def _line(self, line: str) -> None:
        if line[:1] in (".", "'"):
            name, rest = _split_request(line)
            if name:
                self._request(name, rest)
            return
        self._text(line)

    def _text(self, line: str) -> None:
        if not line.strip() and (line == "" or not self.nofill):
            self._blank_line()
            return
        if not self.nofill and line[:1] in " \t" and self.pending_tag is None:
            self._break()
        if self.next_font is not None:
            f = self.next_font
            self.next_font = None
            runs, cont = self._runs(line, [f, f])
        else:
            runs, cont = self._runs(line, self.fs)
        if not runs and self.nofill and not cont and not re.fullmatch(r"(?:\\[{}]|\s)*", line):
            self._blank_line()  # e.g. a `\&` placeholder line inside .EX/.nf
            return
        self._emit(runs, cont)

    # ---------------------------------------------------------------- requests
    def _request(self, name: str, rest: str) -> None:
        h = getattr(self, "_rq_" + name, None) if name.isidentifier() else None
        if h is not None:
            h(rest)
            return
        if name in _MDOC_BLOCK or name in MDOC_INLINE:
            self._mdoc(name, rest)
            return
        if name in self.macros and name not in _BUILTINS:
            self._expand_macro(name, parse_args(rest))
            return
        # unknown request: ignored

    def _expand_macro(self, name: str, args: list[str]) -> None:
        body = self.macros.get(name) or []
        if len(body) > self.budget or self.depth >= MAX_MACRO_DEPTH:
            return
        self.budget -= len(body)

        def repl(m: re.Match) -> str:
            g = m.group(1)
            if g is None:
                return str(len(args))
            if g == "*":
                return " ".join(args)
            if g == "@":
                return " ".join(f'"{a}"' for a in args)
            if g == "0":
                return name
            k = int(g.strip("([]"))
            return args[k - 1] if 0 < k <= len(args) else ""

        lines = [_MACRO_ARG_RE.sub(repl, ln) for ln in body]
        expanded: list[str] = []
        for ln in lines:
            expanded.extend(_logical_lines(ln) or [])
        d = self.depth + 1
        self.queue.extendleft((ln, d) for ln in reversed(expanded))

    # roff layer -------------------------------------------------------
    def _collect_until(self, end: str) -> list[str]:
        body: list[str] = []
        while self.queue:
            ln = self.queue.popleft()[0]
            if ln[:1] in (".", "'"):
                nm, _ = _split_request(ln)
                if nm == end:
                    break
            body.append(ln)
        return body

    def _rq_de(self, rest: str, append: bool = False) -> None:
        args = parse_args(rest)
        if not args:
            return
        end = args[1] if len(args) > 1 else "."
        raw = self._collect_until(end)
        body = [_copy_mode(ln) for ln in raw]
        if append:
            self.macros.setdefault(args[0], []).extend(body)
        else:
            self.macros[args[0]] = body

    def _rq_de1(self, rest: str) -> None:
        self._rq_de(rest)

    def _rq_am(self, rest: str) -> None:
        self._rq_de(rest, append=True)

    def _rq_am1(self, rest: str) -> None:
        self._rq_de(rest, append=True)

    def _rq_ig(self, rest: str) -> None:
        args = parse_args(rest)
        self._collect_until(args[0] if args else ".")

    def _rq_ds(self, rest: str, append: bool = False) -> None:
        m = re.match(r"(\S+)[ \t]*(.*)$", rest)
        if not m:
            return
        val = m.group(2).removeprefix('"')
        if append:
            self.strings[m.group(1)] = self.strings.get(m.group(1), "") + val
        else:
            self.strings[m.group(1)] = val

    def _rq_ds1(self, rest: str) -> None:
        self._rq_ds(rest)

    def _rq_as(self, rest: str) -> None:
        self._rq_ds(rest, append=True)

    def _rq_as1(self, rest: str) -> None:
        self._rq_ds(rest, append=True)

    def _rq_rm(self, rest: str) -> None:
        for a in parse_args(rest):
            self.strings.pop(a, None)
            self.macros.pop(a, None)

    def _rq_rn(self, rest: str) -> None:
        a = parse_args(rest)
        if len(a) >= 2:
            if a[0] in self.macros:
                self.macros[a[1]] = self.macros.pop(a[0])
            if a[0] in self.strings:
                self.strings[a[1]] = self.strings.pop(a[0])

    def _eval_num(self, expr: str) -> float | None:
        def reg(m: re.Match) -> str:
            nm = m.group(1) or m.group(2) or m.group(3) or ""
            return str(REGISTERS.get(nm, 0))

        e = re.sub(r"\\n[+-]?(?:\((..)|\[([^\]]*)\]|(.))", reg, expr)
        e = re.sub(r"\\w'[^']*'", "0", e)
        e = re.sub(r"(?<=\d)[icpPmnvuMsf]", "", e)
        toks = re.findall(r"\d+(?:\.\d*)?|\.\d+|<=|>=|==|!=|<\?|>\?|[-+*/%<>=&:()!]", e)
        if "".join(toks) != re.sub(r"\s+", "", e):
            return None
        pos = 0

        def atom() -> float:
            nonlocal pos
            if pos >= len(toks):
                raise ValueError
            t = toks[pos]
            pos += 1
            if t == "(":
                v = expr_()
                if pos < len(toks) and toks[pos] == ")":
                    pos += 1
                return v
            if t == "-":
                return -atom()
            if t == "+":
                return atom()
            if t == "!":
                return 0.0 if atom() > 0 else 1.0
            return float(t)

        def expr_() -> float:
            nonlocal pos
            v = atom()
            while pos < len(toks) and toks[pos] != ")":
                op = toks[pos]
                pos += 1
                w = atom()
                if op == "+":
                    v += w
                elif op == "-":
                    v -= w
                elif op == "*":
                    v *= w
                elif op == "/":
                    v = v / w if w else 0.0
                elif op == "%":
                    v = v % w if w else 0.0
                elif op == "<":
                    v = float(v < w)
                elif op == ">":
                    v = float(v > w)
                elif op == "<=":
                    v = float(v <= w)
                elif op == ">=":
                    v = float(v >= w)
                elif op in ("=", "=="):
                    v = float(v == w)
                elif op == "!=":
                    v = float(v != w)
                elif op == "&":
                    v = float(v > 0 and w > 0)
                elif op == ":":
                    v = float(v > 0 or w > 0)
                elif op == "<?":
                    v = min(v, w)
                elif op == ">?":
                    v = max(v, w)
            return v

        try:
            return expr_()
        except (ValueError, IndexError, ZeroDivisionError, OverflowError):
            return None

    def _condition(self, rest: str) -> tuple[bool, str]:
        s = rest.lstrip(" \t")
        neg = False
        while s.startswith("!"):
            neg = not neg
            s = s[1:]
        if not s:
            return False, ""
        c = s[0]
        result = False
        if c in "ntoev" and (len(s) == 1 or s[1] in " \t\\"):
            result = c in "no"
            body = s[1:]
        elif c in "cdrmFS" and len(s) > 1 and s[1] in " \t":
            t = s[2:].lstrip(" \t")
            if c == "c":
                if t.startswith("\\["):
                    j = t.find("]")
                    tok_end = len(t) if j < 0 else j + 1
                elif t.startswith("\\("):
                    tok_end = 4
                elif t.startswith("\\"):
                    tok_end = 2
                else:
                    tok_end = 1
                result = True
            else:
                m = re.match(r"\S*", t)
                tok_end = m.end() if m else 0
                nm = t[:tok_end]
                if c in "dm":
                    result = nm in self.strings or nm in self.macros
                elif c == "r":
                    result = nm in REGISTERS
            body = t[tok_end:]
        elif not c.isalnum() and c not in "\\(+-.":
            j = s.find(c, 1)
            k = s.find(c, j + 1) if j >= 0 else -1
            if j < 0 or k < 0:
                return False, ""
            result = self._plain(s[1:j]) == self._plain(s[j + 1:k])
            body = s[k + 1:]
        else:
            m = re.match(r"\S+", s)
            expr = m.group(0) if m else ""
            body = s[len(expr):]
            if expr.endswith("\\{"):
                expr = expr[:-2]
                body = "\\{" + body
            v = self._eval_num(expr)
            result = bool(v is not None and v > 0)
        return (not result) if neg else result, body

    def _cond_body(self, ok: bool, body: str) -> None:
        b = body.lstrip(" \t")
        block = b.startswith("\\{")
        if block:
            b = b[2:].lstrip(" \t")
        if ok:
            if b.strip():
                self._line(b)
            return
        if not block:
            return
        depth = 1 + b.count("\\{") - b.count("\\}")
        while depth > 0 and self.queue:
            ln = self.queue.popleft()[0]
            depth += ln.count("\\{") - ln.count("\\}")

    def _rq_if(self, rest: str) -> None:
        ok, body = self._condition(rest)
        self._cond_body(ok, body)

    def _rq_ie(self, rest: str) -> None:
        ok, body = self._condition(rest)
        self.ie_stack.append(ok)
        self._cond_body(ok, body)

    def _rq_el(self, rest: str) -> None:
        ok = not self.ie_stack.pop() if self.ie_stack else False
        self._cond_body(ok, rest)

    def _rq_ft(self, rest: str) -> None:
        a = parse_args(rest)
        if not a or a[0] in ("P", ""):
            self.fs[0], self.fs[1] = self.fs[1], self.fs[0]
        else:
            self.fs[1] = self.fs[0]
            self.fs[0] = FONTS.get(a[0], FONTS.get(a[0].upper(), "R"))

    def _rq_so(self, rest: str) -> None:
        pass  # inclusion is not followed; alias pages are reported through `so`

    # man structure ----------------------------------------------------
    def _rq_TH(self, rest: str) -> None:
        if self.th_seen:
            return
        self.th_seen = True
        a = [self._plain(x).strip() for x in parse_args(rest)] + [""] * 5
        self.th = {"title": a[0], "section": a[1], "date": a[2], "source": a[3], "manual": a[4]}

    def _rq_SH(self, rest: str) -> None:
        args = parse_args(rest)
        title = self._plain(" ".join(args)).strip() if args else ""
        self._new_section(title)
        if not args:
            self.pending_heading = self.sec
        else:
            self.pending_heading = None

    def _rq_SS(self, rest: str) -> None:
        args = parse_args(rest)
        self._close_item()
        self._container()
        self.stack = [self.stack[0]]
        self.nofill = False
        self.ex = False
        self.sy = False
        ss = {"t": "ss", "id": "", "title": self._plain(" ".join(args)).strip() if args else ""}
        self.stack[0].append(ss)
        self.pending_heading = ss if not args else None

    def _rq_P(self, rest: str) -> None:
        self.pending_heading = None
        if self.nofill and self.item is None and self.pre is not None:
            self.pre["lines"].append([])
            self.cont = False
            return
        self._close_item()

    _rq_PP = _rq_P
    _rq_LP = _rq_P
    _rq_HP = _rq_P

    def _rq_TP(self, rest: str) -> None:
        self.pending_heading = None
        self._close_item()
        tp = {"t": "tp", "tags": [[]], "body": []}
        self._container().append(tp)
        self.item = tp
        self.pending_tag = tp

    def _rq_TQ(self, rest: str) -> None:
        if self.item is not None and self.item.get("t") == "tp" and not self.item["body"]:
            self.item["tags"].append([])
            self.pending_tag = self.item
            self._reset_flow()
        else:
            self._rq_TP(rest)

    def _rq_IP(self, rest: str) -> None:
        self.pending_heading = None
        args = parse_args(rest)
        self._close_item()
        tag: list[dict[str, Any]] = []
        if args:
            tag, _ = self._runs(args[0], list(self.fs))
        if plain_text(tag).strip():
            blk: dict[str, Any] = {"t": "tp", "tags": [tag], "body": []}
        else:
            blk = {"t": "ip", "body": []}
        self._container().append(blk)
        self.item = blk

    def _rq_RS(self, rest: str) -> None:
        self.pending_heading = None
        self._close_item()
        a = parse_args(rest)
        if a and self._eval_num(a[0]) == 0:
            self.stack.append(self._container())  # `.RS 0`: new level without extra indent
            return
        rs = {"t": "rs", "body": []}
        self._container().append(rs)
        self.stack.append(rs["body"])

    def _rq_RE(self, rest: str) -> None:
        self._close_item()
        if len(self.stack) > 1:
            self.stack.pop()

    def _rq_nf(self, rest: str) -> None:
        self.nofill = True
        self.ex = False
        self._reset_flow()

    def _rq_fi(self, rest: str) -> None:
        self.nofill = False
        self.ex = False
        self._reset_flow()

    def _rq_EX(self, rest: str) -> None:
        self.nofill = True
        self.ex = True
        self._reset_flow()

    def _rq_EE(self, rest: str) -> None:
        self._rq_fi(rest)

    def _rq_br(self, rest: str) -> None:
        self._break()

    def _rq_sp(self, rest: str) -> None:
        self._blank_line()

    def _font_macro(self, name: str, rest: str) -> None:
        args = parse_args(rest)
        if name in ("B", "I", "SB", "SM"):
            f = {"B": "B", "I": "I", "SB": "B"}.get(name, self.fs[0])
            if not args:
                self.next_font = f
                return
            runs, cont = self._runs(" ".join(args), [f, f])
            self._emit(runs, cont)
            return
        fonts = [name[0], name[1]]
        runs = []
        cont = False
        for k, a in enumerate(args):
            f = fonts[k % 2]
            r, c = self._runs(a, [f, f])
            for x in r:
                _append(runs, x)
            if c:
                cont = True
                break
        self._emit(runs, cont)

    def _rq_B(self, rest: str) -> None:
        self._font_macro("B", rest)

    def _rq_I(self, rest: str) -> None:
        self._font_macro("I", rest)

    def _rq_SB(self, rest: str) -> None:
        self._font_macro("SB", rest)

    def _rq_SM(self, rest: str) -> None:
        self._font_macro("SM", rest)

    def _rq_BR(self, rest: str) -> None:
        self._font_macro("BR", rest)

    def _rq_BI(self, rest: str) -> None:
        self._font_macro("BI", rest)

    def _rq_IB(self, rest: str) -> None:
        self._font_macro("IB", rest)

    def _rq_IR(self, rest: str) -> None:
        self._font_macro("IR", rest)

    def _rq_RB(self, rest: str) -> None:
        self._font_macro("RB", rest)

    def _rq_RI(self, rest: str) -> None:
        self._font_macro("RI", rest)

    def _rq_MR(self, rest: str) -> None:
        a = parse_args(rest)
        if not a:
            return
        name = self._plain(a[0]).strip()
        sec = self._plain(a[1]).strip() if len(a) > 1 else ""
        r = self._mk(name, "B")
        runs = [r]
        if sec:
            r["ref"] = [name, sec]
            runs.append(self._mk(f"({sec})", "R"))
        if len(a) > 2:
            t, _ = self._runs(a[2], ["R", "R"])
            runs.extend(t)
        self._emit(runs)

    def _rq_UR(self, rest: str) -> None:
        a = parse_args(rest)
        self.link = self._plain(a[0]).strip() if a else ""
        self.link_mark = self.emitted

    def _rq_MT(self, rest: str) -> None:
        a = parse_args(rest)
        addr = self._plain(a[0]).strip() if a else ""
        self.link = "mailto:" + addr
        self.link_mark = self.emitted

    def _close_link(self, rest: str) -> None:
        link = self.link
        if link is None:
            return
        if self.emitted == self.link_mark and link:
            text = link.removeprefix("mailto:")
            self._emit([{"x": text, "url": link}])
        self.link = None
        a = parse_args(rest)
        if a:
            r, c = self._runs(a[0], ["R", "R"])
            self._emit(r, c, nospace=True)

    def _rq_UE(self, rest: str) -> None:
        self._close_link(rest)

    def _rq_ME(self, rest: str) -> None:
        self._close_link(rest)

    def _rq_SY(self, rest: str) -> None:
        a = parse_args(rest)
        if not self.nofill:
            if self.pending_tag is not None:
                self._close_item()
            self.para = None
        self.sy = True
        self.sy_line = False
        self.cont = False
        if a:
            runs, cont = self._runs(a[0], ["B", "B"])
            self._emit(runs, cont)
        self.sy_line = True

    def _rq_OP(self, rest: str) -> None:
        a = parse_args(rest)
        if not a:
            return
        runs = [self._mk("[", "R")]
        r, _ = self._runs(a[0], ["B", "B"])
        runs.extend(r)
        if len(a) > 1:
            runs.append(self._mk(" ", "R"))
            r, _ = self._runs(" ".join(a[1:]), ["I", "I"])
            runs.extend(r)
        runs.append(self._mk("]", "R"))
        self._emit(runs)

    def _rq_YS(self, rest: str) -> None:
        self.sy = False
        self.sy_line = False
        self.cont = False
        if not self.nofill:
            self.para = None

    def _rq_TS(self, rest: str) -> None:
        raw = self._collect_until("TE")
        self.pending_heading = None
        try:
            tbl = self._table(raw)
        except Exception:
            if _RAISE:
                raise
            tbl = None
        if tbl is None:
            return
        self.para = None
        self.pre = None
        self.cont = False
        if self.pending_tag is not None:
            self.pending_tag = None
        self._target().append(tbl)

    # ---------------------------------------------------------------- tbl
    def _fragment(self, lines: list[str], font: str) -> list[dict[str, Any]]:
        sub = _Parser("", self.strings, self.macros)
        sub.queue = deque((ln, self.depth) for ln in lines)
        sub.budget = min(self.budget, 20_000)
        sub.fs = [font, font]
        sub.sec = {"id": "", "title": "", "blocks": []}
        sub.stack = [sub.sec["blocks"]]
        sub.run_lines()
        runs: list[dict[str, Any]] = []
        for blk in _walk(sub.sec["blocks"]):
            if blk["t"] == "p":
                part = blk["c"]
            elif blk["t"] == "pre":
                part = []
                for ln in blk["lines"]:
                    if part:
                        part.append({"br": 1})
                    part.extend(ln)
            elif blk["t"] == "tp":
                part = []
                for tg in blk["tags"]:
                    part.extend(tg)
                    part.append({"x": " "})
            else:
                continue
            if not part:
                continue
            if runs:
                runs.append({"br": 1})
            runs.extend(part)
        return runs

    def run_lines(self) -> None:
        while self.queue:
            line, self.depth = self.queue.popleft()
            self._line(line)

    @staticmethod
    def _fmt_row(part: str) -> list[list[str]]:
        cols: list[list[str]] = []
        j = 0
        n = len(part)
        keymap = {"l": "l", "c": "c", "r": "r", "n": "r", "a": "l", "s": "s", "^": "^", "_": "_", "=": "_",
                  "-": "_"}
        while j < n:
            ch = part[j]
            lc = ch.lower()
            if lc in keymap:
                cols.append([keymap[lc], "R"])
                j += 1
                continue
            if not cols:
                j += 1
                continue
            if lc == "b":
                cols[-1][1] = "BI" if cols[-1][1] == "I" else "B"
            elif lc == "i":
                cols[-1][1] = "BI" if cols[-1][1] == "B" else "I"
            elif lc == "f":
                j += 1
                if j < n and part[j] == "(":
                    fn = part[j + 1:j + 3]
                    j += 2
                elif j + 1 < n and part[j:j + 2] in ("CW", "CR", "BI", "CB", "CI"):
                    fn = part[j:j + 2]
                    j += 1
                else:
                    fn = part[j:j + 1]
                cols[-1][1] = FONTS.get(fn, "R")
            elif lc == "w":
                if j + 1 < n and part[j + 1] == "(":
                    k = part.find(")", j)
                    j = n - 1 if k < 0 else k
                else:
                    while j + 1 < n and (part[j + 1].isdigit() or part[j + 1] in ".n"):
                        j += 1
            elif lc in "pv":
                while j + 1 < n and (part[j + 1].isdigit() or part[j + 1] in "+-"):
                    j += 1
            j += 1
        return cols

    def _read_formats(self, raw: list[str], i: int) -> tuple[list[list[list[str]]], int]:
        fmts: list[list[list[str]]] = []
        while i < len(raw):
            s = raw[i].strip()
            i += 1
            end = s.endswith(".")
            if end:
                s = s[:-1]
            for part in s.split(","):
                cols = self._fmt_row(part)
                if cols:
                    fmts.append(cols)
            if end:
                break
        return fmts or [[["l", "R"]]], i

    def _table(self, raw: list[str]) -> dict[str, Any] | None:
        i = 0
        tab = "\t"
        box = False
        if raw and raw[0].rstrip().endswith(";"):
            opts = raw[0].lower()
            box = bool(re.search(r"\b(all)?box\b|\bframe\b|\bdoublebox\b|\bdoubleframe\b", opts))
            m = re.search(r"tab\s*\((.)\)", raw[0])
            if m:
                tab = m.group(1)
            i = 1
        fmts, i = self._read_formats(raw, i)
        all_fmts = list(fmts)
        rows: list[list[list[dict[str, Any]]]] = []
        th_header: int | None = None
        first_rule: int | None = None
        fmt_base = 0
        while i < len(raw):
            line = raw[i]
            i += 1
            if line[:1] in (".", "'"):
                nm, _ = _split_request(line)
                if nm == "T&":
                    fmts, i = self._read_formats(raw, i)
                    fmt_base = len(rows)
                elif nm == "TH":
                    th_header = len(rows)
                continue
            if line.strip() in ("_", "=", "\\_", "-"):
                if first_rule is None:
                    first_rule = len(rows)
                continue
            fmt = fmts[min(len(rows) - fmt_base, len(fmts) - 1)]
            cells_raw: list[tuple[str, Any]] = []
            pending: str | None = line
            while pending is not None:
                parts = pending.split(tab)
                pending = None
                for idx, p in enumerate(parts):
                    if p.rstrip().endswith("T{") and idx == len(parts) - 1:
                        block: list[str] = []
                        closed = None
                        while i < len(raw):
                            ln = raw[i]
                            i += 1
                            if ln.startswith("T}"):
                                closed = ln[2:]
                                break
                            block.append(ln)
                        pre_text = p.rstrip()[:-2]
                        if pre_text.strip():
                            block.insert(0, pre_text)
                        cells_raw.append(("block", block))
                        if closed:
                            pending = closed[len(tab):] if closed.startswith(tab) else None
                        break
                    cells_raw.append(("text", p))
            row: list[list[dict[str, Any]]] = []
            for k, (kind, val) in enumerate(cells_raw):
                font = fmt[k][1] if k < len(fmt) else "R"
                if kind == "block":
                    runs = self._fragment(val, font)
                else:
                    v = val.strip()
                    if v in ("\\_", "_", "\\^", "=", "\\=", "\\R_"):
                        runs = []
                    else:
                        runs, _ = self._runs(val, [font, font])
                row.append(_normalize_runs(runs, strip=True))
            rows.append(row)
        if not rows:
            return None
        last = fmts[-1]
        ncols = max([len(last)] + [len(r) for r in rows])
        align = []
        for k in range(ncols):
            a = last[k][0] if k < len(last) else "l"
            align.append(a if a in ("l", "c", "r") else "l")
        if th_header is not None:
            header = th_header
        elif first_rule is not None and 1 <= first_rule <= 3 and len(rows) > first_rule:
            header = first_rule
        elif len(all_fmts) > 1 and all(c[0] in ("s", "c") for c in all_fmts[0][1:]) \
                and any(c[0] == "s" for c in all_fmts[0]) and len(rows) > 1:
            header = 1  # spanned title row
        elif len(all_fmts) > 1 and any(c[1] in ("B", "BI") for c in all_fmts[0]) and len(rows) > 1 \
                and all_fmts[0] != all_fmts[1]:
            header = 1
        else:
            header = 0
        return {"t": "table", "align": align, "box": box, "rows": rows, "header": header}

    # ---------------------------------------------------------------- mdoc subset
    def _mdoc(self, name: str, rest: str) -> None:
        args = parse_args(rest)
        if name == "Dd":
            self.th["date"] = self._plain(" ".join(args)).strip()
            return
        if name == "Dt":
            if args:
                self.th["title"] = self._plain(args[0])
            if len(args) > 1:
                self.th["section"] = self._plain(args[1])
            return
        if name == "Os":
            self.th["source"] = self._plain(" ".join(args)).strip()
            return
        if name == "Sh":
            self._rq_SH(rest)
            return
        if name == "Ss":
            self._rq_SS(rest)
            return
        if name in ("Pp", "Lp"):
            self._rq_P(rest)
            return
        if name == "Bl":
            kind = next((a[1:] for a in args if a.startswith("-") and a[1:] in _MDOC_LIST_TYPES), "tag")
            pushed = bool(self.mdoc_lists) or "-offset" in args
            if pushed:
                self._rq_RS("")
            else:
                self._close_item()
            self.mdoc_lists.append({"type": kind, "n": 0, "rs": pushed})
            return
        if name == "El":
            if self.mdoc_lists:
                lst = self.mdoc_lists.pop()
                if lst["rs"]:
                    self._rq_RE("")
                else:
                    self._close_item()
            return
        if name == "It":
            lst = self.mdoc_lists[-1] if self.mdoc_lists else {"type": "tag", "n": 0}
            lst["n"] += 1
            kind = lst["type"]
            self._close_item()
            if kind in ("bullet", "dash", "hyphen", "enum"):
                tag = [{"x": {"bullet": "\u2022", "enum": f"{lst['n']}."}.get(kind, "-")}]
            elif kind == "item":
                tag = []
            elif kind == "column":
                tag = self._mdoc_runs([a for a in args if a not in ("Ta",)])
            else:
                tag = self._mdoc_runs(args)
            if kind == "column":
                blk: dict[str, Any] = {"t": "ip", "body": [{"t": "p", "c": tag}]}
            elif plain_text(tag).strip():
                blk = {"t": "tp", "tags": [tag], "body": []}
            else:
                blk = {"t": "ip", "body": []}
            self._container().append(blk)
            self.item = blk
            return
        if name == "Bd":
            literal = any(a in ("-literal", "-unfilled") for a in args)
            pushed = "-offset" in args
            if pushed:
                self._rq_RS("")
            self.mdoc_bd.append((literal, pushed))
            if literal:
                self._rq_nf("")
            else:
                self._reset_flow()
            return
        if name == "Ed":
            if self.mdoc_bd:
                literal, pushed = self.mdoc_bd.pop()
                if literal:
                    self._rq_fi("")
                if pushed:
                    self._rq_RE("")
            return
        if name in ("D1", "Dl"):
            self._rq_RS("")
            if name == "Dl":
                self._rq_nf("")
            self._emit(self._mdoc_runs(args))
            if name == "Dl":
                self._rq_fi("")
            self._rq_RE("")
            return
        if name == "Nm" and args and self.sec is not None and self.sec.get("title", "").upper() == "NAME":
            nm = self._plain(args[0])
            if not self.mdoc_name:
                self.mdoc_name = nm
        elif name == "Nm" and args and not self.mdoc_name:
            self.mdoc_name = self._plain(args[0])
        if name == "Nd":
            self._emit(self._mdoc_runs(["\\-"] + args))
            return
        if name in ("Bk", "Ek", "Sm", "Rs", "Re", "Bf", "Ef", "Xo"):
            return
        in_synopsis = self.sec is not None and self.sec.get("title", "").upper() == "SYNOPSIS"
        # mdoc SYNOPSIS: one declaration per line (`.Fd`/`.In`/`.Ft` start a line, `.Fn` ends with ";")
        if in_synopsis and not self.nofill and self.item is None and (
                name in ("Fd", "In", "Ft") or (name in ("Fn", "Fo") and self.mdoc_last != "Ft")):
            self.para = None
        runs = self._mdoc_runs([name] + args)
        if in_synopsis and name in ("Fn", "Fc") and runs:
            runs.append({"x": ";"})
        self._emit(runs, nospace=(name in ("Xc", "Ns")))
        self.mdoc_last = name

    def _mdoc_runs(self, toks: list[str]) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        font = "R"
        nospace = True
        closers: list[str] = []
        flag = False
        i = 0

        def word(text: str, f: str, ref: list[str] | None = None) -> None:
            nonlocal nospace
            if not text:
                return
            if runs and not nospace and not re.fullmatch(r"[.,:;)\]?!]", text):
                _append(runs, self._mk(" ", "R"))
            r, _ = self._runs(text, [f, f])
            if ref and r:
                r[0]["ref"] = ref
            for x in r:
                _append(runs, x)
            nospace = text in ("(", "[")

        while i < len(toks):
            t = toks[i]
            i += 1
            if t in MDOC_INLINE:
                flag = False
                if t == "Xr":
                    nm = toks[i] if i < len(toks) else ""
                    sec = toks[i + 1] if i + 1 < len(toks) and re.fullmatch(r"\d\w*", toks[i + 1]) else ""
                    i += 1 + (1 if sec else 0)
                    word(nm, "B", [self._plain(nm), sec] if sec else None)
                    if sec:
                        _append(runs, self._mk(f"({sec})", "R"))
                    font = "R"
                elif t == "Ns":
                    nospace = True
                elif t == "Pf":
                    if i < len(toks):
                        word(toks[i], "R")
                        i += 1
                    nospace = True
                elif t in _MDOC_ENCLOSE:
                    o, c = _MDOC_ENCLOSE[t]
                    word(o, "R")
                    nospace = True
                    closers.append(c)
                elif t in _MDOC_OPENCLOSE:
                    val, is_open = _MDOC_OPENCLOSE[t]
                    if is_open:
                        word(val, "R")
                        nospace = True
                    else:
                        nospace = True
                        word(val, "R")
                        nospace = False
                elif t in _MDOC_WORDS:
                    word(_MDOC_WORDS[t], "R")
                    if i < len(toks) and t == "Bx" and re.fullmatch(r"[\d.]+\w*", toks[i]):
                        nospace = True
                        word(toks[i], "R")
                        i += 1
                elif t == "Nm":
                    font = "B"
                    if i >= len(toks) or toks[i] in MDOC_INLINE or re.fullmatch(r"[.,:;)\]?!(\[]+", toks[i]):
                        word(self.mdoc_name, "B")
                elif t == "Fn":
                    if i < len(toks):
                        word(toks[i], "B")
                        i += 1
                    nospace = True
                    word("(", "R")
                    first = True
                    end = len(toks)
                    while end > i and re.fullmatch(r"[.,:;)\]?!]", toks[end - 1]):
                        end -= 1
                    while i < end and toks[i] not in MDOC_INLINE:
                        if not first:
                            nospace = True
                            word(",", "R")
                        word(toks[i], "I")
                        first = False
                        i += 1
                    nospace = True
                    word(")", "R")
                    font = "R"
                elif t == "Fo":
                    if i < len(toks):
                        word(toks[i], "B")
                        i += 1
                    nospace = True
                    word("(", "R")
                elif t == "In":
                    if i < len(toks):
                        hdr = toks[i]
                        i += 1
                        insyn = self.sec is not None and self.sec.get("title", "").upper() == "SYNOPSIS"
                        word(("#include <%s>" if insyn else "<%s>") % hdr, "B")
                elif t == "Fc":
                    nospace = True
                    word(")", "R")
                elif t == "Fl":
                    flag = True
                    font = "B"
                    if i >= len(toks) or toks[i] in MDOC_INLINE:
                        word("-", "B")
                elif t == "St":
                    if i < len(toks):
                        word(_MDOC_STD.get(toks[i], toks[i]), "R")
                        i += 1
                elif t == "Rv":
                    word("The function returns the value 0 if successful; otherwise the value -1 is returned "
                         "and the global variable errno is set to indicate the error.", "R")
                    i = len(toks)
                elif t == "Ex":
                    word("The utility exits 0 on success, and >0 if an error occurs.", "R")
                    i = len(toks)
                else:
                    font = MDOC_INLINE.get(t, "R") or "R"
                continue
            if re.fullmatch(r"[.,:;)\]?!]+", t):
                word(t, "R")
                continue
            if flag:
                word("-" + t if not t.startswith("-") else t, "B")
                continue
            word(t, font)
        for c in reversed(closers):
            nospace = True
            word(c, "R")
        return runs

    # ---------------------------------------------------------------- finish
    def _finish(self) -> dict[str, Any]:
        sections = []
        for s in self.sections:
            s["blocks"] = _normalize_blocks(s["blocks"])
            if s is self.sections[0] and s["id"] == "" and s["title"] == "" and not s["blocks"]:
                continue
            sections.append(s)
        used: set[str] = set()
        for idx, s in enumerate(sections):
            if idx == 0 and not s["title"]:
                s["id"] = ""
                used.add("")
                continue
            base = _slug(s["title"]) or "section"
            sid = base
            k = 2
            while sid in used:
                sid = f"{base}-{k}"
                k += 1
            s["id"] = sid
            used.add(sid)
        for s in sections:
            for blk in _walk(s["blocks"]):
                if blk.get("t") != "ss":
                    continue
                base = _slug(blk["title"]) or "subsection"
                if base in used and s["id"]:
                    base = f"{s['id']}-{base}"
                sid = base
                k = 2
                while sid in used:
                    sid = f"{base}-{k}"
                    k += 1
                blk["id"] = sid
                used.add(sid)
        return {"format": FORMAT, "th": self.th, "so": None, "sections": sections}


# --------------------------------------------------------------------------------------------------------------
# mdoc tables
# --------------------------------------------------------------------------------------------------------------
MDOC_INLINE: dict[str, str] = {
    "Fa": "I", "Ft": "I", "Fd": "B", "In": "B", "Ar": "I", "Fl": "B", "Em": "I", "Sy": "B", "Dv": "R",
    "Er": "R", "Ev": "R", "Pa": "I", "Li": "CR", "Ql": "CR", "Sx": "R", "Va": "I", "Cm": "B", "Ic": "B",
    "Ad": "I", "An": "R", "Tn": "R", "No": "R", "Vt": "I", "Cd": "B", "Ms": "R", "Lb": "R", "Ot": "I",
    "Mt": "R", "Lk": "R", "Xr": "", "Ns": "", "Pf": "", "Nm": "B", "Fn": "", "Fo": "", "Fc": "", "St": "",
    "Rv": "", "Ex": "", "Xc": "", "Ux": "", "Bx": "", "At": "", "Nx": "", "Fx": "", "Ox": "", "Bsx": "",
    "Dx": "", "Lx": "", "Dq": "", "Pq": "", "Aq": "", "Sq": "", "Bq": "", "Qq": "", "Op": "", "Brq": "",
    "Do": "", "Dc": "", "Po": "", "Pc": "", "Ao": "", "Ac": "", "So": "", "Sc": "", "Bo": "", "Bc": "",
    "Qo": "", "Qc": "", "Oo": "", "Oc": "", "Bro": "", "Brc": "", "Eo": "", "Ec": "", "%A": "R", "%B": "I",
    "%D": "R", "%I": "R", "%J": "I", "%N": "R", "%O": "R", "%P": "R", "%R": "R", "%T": "I", "%V": "R",
    "Ta": "",
}
_MDOC_BLOCK = {"Dd", "Dt", "Os", "Sh", "Ss", "Pp", "Lp", "Bl", "El", "It", "Bd", "Ed", "D1", "Dl", "Nd",
               "Bk", "Ek", "Sm", "Rs", "Re", "Bf", "Ef", "Xo"}
_MDOC_LIST_TYPES = {"bullet", "dash", "hyphen", "enum", "item", "tag", "ohang", "inset", "hang", "diag",
                    "column"}
_MDOC_ENCLOSE = {"Dq": ("\u201c", "\u201d"), "Pq": ("(", ")"), "Aq": ("<", ">"), "Sq": ("\u2018", "\u2019"),
                 "Bq": ("[", "]"), "Qq": ('"', '"'), "Op": ("[", "]"), "Brq": ("{", "}"),
                 "Ql": ("\u2018", "\u2019")}
_MDOC_OPENCLOSE = {"Do": ("\u201c", True), "Dc": ("\u201d", False), "Po": ("(", True), "Pc": (")", False),
                   "Ao": ("<", True), "Ac": (">", False), "So": ("\u2018", True), "Sc": ("\u2019", False),
                   "Bo": ("[", True), "Bc": ("]", False), "Qo": ('"', True), "Qc": ('"', False),
                   "Oo": ("[", True), "Oc": ("]", False), "Bro": ("{", True), "Brc": ("}", False),
                   "Eo": ("", True), "Ec": ("", False)}
_MDOC_WORDS = {"Ux": "UNIX", "Bx": "BSD", "At": "AT&T UNIX", "Nx": "NetBSD", "Fx": "FreeBSD",
               "Ox": "OpenBSD", "Bsx": "BSD/OS", "Dx": "DragonFly", "Lx": "Linux"}
_MDOC_STD = {"-ansiC": "ANSI X3.159-1989 (\u201cANSI C89\u201d)", "-isoC": "ISO/IEC 9899:1990 (\u201cISO C90\u201d)",
             "-isoC-99": "ISO/IEC 9899:1999 (\u201cISO C99\u201d)", "-p1003.1": "IEEE Std 1003.1 (\u201cPOSIX.1\u201d)",
             "-p1003.1-2001": "IEEE Std 1003.1-2001 (\u201cPOSIX.1\u201d)", "-p1003.2": "IEEE Std 1003.2 (\u201cPOSIX.2\u201d)",
             "-susv2": "Version 2 of the Single UNIX Specification (\u201cSUSv2\u201d)",
             "-xpg4.2": "X/Open Portability Guide Issue 4, Version 2 (\u201cXPG4.2\u201d)"}
_MDOC_ENCLOSE.pop("Ql")  # Ql is a font macro (literal) in MDOC_INLINE


# --------------------------------------------------------------------------------------------------------------
# Normalization helpers
# --------------------------------------------------------------------------------------------------------------
def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return a.get("f") == b.get("f") and a.get("ref") == b.get("ref") and a.get("url") == b.get("url") \
        and "br" not in a and "br" not in b


def _append(runs: list[dict[str, Any]], r: dict[str, Any]) -> None:
    if runs and _same(runs[-1], r) and "ref" not in r:
        runs[-1]["x"] += r["x"]
    else:
        runs.append(r)


def _normalize_runs(runs: list[dict[str, Any]], strip: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in runs:
        if "br" in r:
            out.append({"br": 1})
            continue
        x = r.get("x", "")
        if not x:
            continue
        if out and _same(out[-1], r):
            out[-1] = dict(out[-1])
            out[-1]["x"] += x
        else:
            out.append(dict(r))
    # cross references: bold/italic name followed by "(section)"
    for k in range(len(out) - 1):
        r = out[k]
        if "ref" in r or "br" in r or r.get("f") not in ("B", "I", "BI", "CR"):
            continue
        nxt = out[k + 1]
        if "br" in nxt:
            continue
        m = REF_SEC_RE.match(nxt.get("x", ""))
        name = r["x"].replace("\u00a0", " ").strip()
        if m and REF_NAME_RE.match(name) and r["x"] == r["x"].rstrip():
            r["ref"] = [name, m.group(1)]
    if strip:
        while out and "br" in out[0]:
            out.pop(0)
        while out and "br" in out[-1]:
            out.pop()
        if out:
            out[0]["x"] = out[0]["x"].lstrip(" \t")
            if not out[0]["x"]:
                out.pop(0)
        if out:
            out[-1]["x"] = out[-1]["x"].rstrip(" \t")
            if not out[-1]["x"]:
                out.pop()
    return out


def _has_text(runs: list[dict[str, Any]]) -> bool:
    return any(r.get("x", "").strip() for r in runs)


def _normalize_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in blocks:
        t = b.get("t")
        if t == "p":
            c = _normalize_runs(b["c"], strip=True)
            if _has_text(c):
                out.append({"t": "p", "c": c})
        elif t == "pre":
            lines = [_normalize_runs(ln, strip=False) for ln in b["lines"]]
            while lines and not _has_text(lines[0]):
                lines.pop(0)
            while lines and not _has_text(lines[-1]):
                lines.pop()
            if lines:
                out.append({"t": "pre", "ex": bool(b.get("ex")), "lines": lines})
        elif t == "tp":
            tags = [_normalize_runs(tg, strip=True) for tg in b["tags"]]
            tags = [tg for tg in tags if _has_text(tg)]
            body = _normalize_blocks(b["body"])
            if tags:
                out.append({"t": "tp", "tags": tags, "body": body})
            elif body:
                out.append({"t": "ip", "body": body})
        elif t in ("ip", "rs"):
            body = _normalize_blocks(b["body"])
            if body:
                out.append({"t": t, "body": body})
        elif t in ("ss", "table"):
            out.append(b)
    return out


def _walk(blocks: list[dict[str, Any]]):
    for b in blocks:
        yield b
        if "body" in b:
            yield from _walk(b["body"])


def walk_blocks(blocks: list[dict[str, Any]]):
    """Depth-first iteration over a block list (nested `tp`/`ip`/`rs` bodies included)."""
    return _walk(blocks)


# --------------------------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------------------------
def so_target(text: str) -> str | None:
    """Target of a `.so`-only page (comments and blank lines aside), else None."""
    if not text or ".so" not in text:
        return None
    try:
        target = None
        for ln in _logical_lines(text):
            if not ln.strip():
                continue
            if ln[:1] in (".", "'"):
                name, rest = _split_request(ln)
                if not name:
                    continue
                if name == "so" and target is None:
                    a = parse_args(rest)
                    if not a:
                        return None
                    target = a[0].strip()
                    continue
            return None
        return target or None
    except Exception:
        if _RAISE:
            raise
        return None


def _empty_doc() -> dict[str, Any]:
    return {"format": FORMAT, "th": {"title": "", "section": "", "date": "", "source": "", "manual": ""},
            "so": None, "sections": []}


def parse(text: str) -> dict[str, Any]:
    """Document model (format 1) of a man page; never raises."""
    if not isinstance(text, str):
        try:
            text = bytes(text or b"").decode("utf-8", "replace")
        except (TypeError, ValueError):
            text = ""
    so = so_target(text)
    if so is not None:
        doc = _empty_doc()
        doc["so"] = so
        return doc
    try:
        doc = _Parser(text).run()
    except Exception:
        if _RAISE:
            raise
        doc = _empty_doc()
        lines = [[{"x": ln}] if ln else [] for ln in text.replace("\r\n", "\n").split("\n")]
        doc["sections"] = [{"id": "", "title": "", "blocks": [{"t": "pre", "ex": False, "lines": lines}]}]
    return doc
