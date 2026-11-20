"""parser/include_graph.py - `#include` resolution and reachability for one ARCH's build layout.

`IncludeResolver` resolves an `#include` the way the compiler does in `make ARCH=<arch>`: a quoted
name is first looked up in the including file's directory, then every name in the `-I` directories of
the build layout (`ArchLayout.includes`, parser/arch_layout.py) in order. A candidate path goes
through the symlinks the build creates (`include/asm/` -> `include/asm-x86_64/` in 2.6.x) and the
generated asm-generic wrappers (`arch/x86/include/generated/asm/bug.h` -> `include/asm-generic/bug.h`)
before it is checked against the revision's files. Headers generated in other ways (`asm-offsets.h`,
`autoconf.h`, syscall tables) are not in the tree and stay unresolved.

`reachable()` walks the includes of a file transitively, after the layout's forced `-include` files:
the files its compilation can read, each with the file that included it (for showing the chain).
Every `#include` line counts, whatever `#if` block it sits in, unless `filter_cond` rejects the line's
conditions (`include_directives_with_conditions()`, `ConditionEvaluator`); without a filter the walk
over-approximates what one configuration reads.
"""
from __future__ import annotations

import posixpath
import re
from collections.abc import Callable

from parser.arch_layout import ArchLayout

_MAX_LINK_HOPS = 8
_COMMENT_OR_STRING_RE = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\\n])*"', re.DOTALL)
_DIRECTIVE_RE = re.compile(r"^[ \t]*#[ \t]*([A-Za-z_]+)[ \t]*(.*)$", re.DOTALL)
_INCLUDE_ARG_RE = re.compile(r'^([<"])([^>"\n]+)[>"]')


def _strip_comments(text: str) -> str:
    """`text` with `/* */` and `//` comments replaced by one space (string literals kept)."""
    def repl(m: re.Match) -> str:
        return m.group(0) if m.group(0).startswith('"') else " "
    return _COMMENT_OR_STRING_RE.sub(repl, text)


def _logical_lines(text: str) -> list[str]:
    """Comment-free source lines with backslash continuations joined (lines split on `\\n` only)."""
    flat = _strip_comments((text or "").replace("\r\n", "\n")).replace("\\\n", " ")
    return flat.split("\n")


def include_directives(content: str) -> list[tuple[str, bool]]:
    """(name, quoted) of every `#include` / `#include_next` with a literal name, in file order."""
    return [(name, quoted) for name, quoted, _conds in include_directives_with_conditions(content)]


def include_directives_with_conditions(content: str) -> list[tuple[str, bool, list[str]]]:
    """(name, quoted, conds) of every `#include` / `#include_next` with a literal name, in file order.

    `conds` lists the conditions that must hold for the include to be read, one string expression per
    enclosing `#if` branch: `#ifdef X` is `defined(X)`, `#ifndef X` is `!defined(X)`, `#if E` is `(E)`;
    a branch after `#elif` / `#else` adds the negation of every earlier branch of its block.
    Directives inside comments are ignored."""
    if "include" not in (content or ""):
        return []
    results: list[tuple[str, bool, list[str]]] = []
    # one frame per open #if block: [earlier branch conditions, active conditions of the current branch]
    stack: list[list[list[str]]] = []
    for line in _logical_lines(content):
        m = _DIRECTIVE_RE.match(line)
        if not m:
            continue
        name, arg = m.group(1), m.group(2).strip()
        if name in ("if", "ifdef", "ifndef"):
            positive = f"({arg})" if name == "if" else (f"defined({arg})" if name == "ifdef" else f"!defined({arg})")
            if name != "if" and not arg:
                positive = "(0)"
            stack.append([[positive], [positive]])
        elif name in ("elif", "elifdef", "elifndef"):
            if not stack:
                continue
            earlier = stack[-1][0]
            positive = f"({arg})" if name == "elif" else (f"defined({arg})" if name == "elifdef" else f"!defined({arg})")
            stack[-1][1] = [f"!({e})" for e in earlier] + [positive]
            earlier.append(positive)
        elif name == "else":
            if stack:
                stack[-1][1] = [f"!({e})" for e in stack[-1][0]]
        elif name == "endif":
            if stack:
                stack.pop()
        elif name in ("include", "include_next"):
            am = _INCLUDE_ARG_RE.match(arg)
            if am:
                conds = [c for frame in stack for c in frame[1]]
                results.append((am.group(2).strip(), am.group(1) == '"', conds))
    return results


# --- Tri-state `#if` expression evaluation -------------------------------------------------------

_TOKEN_RE = re.compile(
    r"\s*(?:(0[xX][0-9a-fA-F]+|\d+)[uUlL]*|('(?:\\.|[^'\\])')|([A-Za-z_]\w*)"
    r"|(<<|>>|<=|>=|==|!=|&&|\|\||[-+*/%<>&^|!~?:(),]))"
)
_BINARY_LEVELS: tuple[tuple[str, ...], ...] = (
    ("||",), ("&&",), ("|",), ("^",), ("&",), ("==", "!="), ("<", ">", "<=", ">="), ("<<", ">>"), ("+", "-"), ("*", "/", "%"),
)
_BINARY_FUNCS: dict[str, Callable[[int, int], int]] = {
    "|": lambda a, b: a | b, "^": lambda a, b: a ^ b, "&": lambda a, b: a & b,
    "==": lambda a, b: int(a == b), "!=": lambda a, b: int(a != b),
    "<": lambda a, b: int(a < b), ">": lambda a, b: int(a > b), "<=": lambda a, b: int(a <= b), ">=": lambda a, b: int(a >= b),
    "<<": lambda a, b: a << b if 0 <= b < 128 else 0, ">>": lambda a, b: a >> b if b >= 0 else 0,
    "+": lambda a, b: a + b, "-": lambda a, b: a - b, "*": lambda a, b: a * b,
}
_UNKNOWN = None   # the tri-state's third value; the others are ints


class _ExprError(Exception):
    pass


def _tokenize(expr: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    expr = expr.rstrip()
    while pos < len(expr):
        m = _TOKEN_RE.match(expr, pos)
        if not m or m.end() == pos:
            raise _ExprError(expr)
        if m.group(1) is not None:
            tokens.append(("num", str(int(m.group(1), 0) if m.group(1)[:2].lower() == "0x" else int(m.group(1), 10))))
        elif m.group(2) is not None:
            tokens.append(("num", str(ord(m.group(2)[1]) if m.group(2)[1] != "\\" else 0)))
        elif m.group(3) is not None:
            tokens.append(("id", m.group(3)))
        else:
            tokens.append(("op", m.group(4)))
        pos = m.end()
    return tokens


class ConditionEvaluator:
    """Evaluates the `conds` of `include_directives_with_conditions()` against a set of defines.

    `defines` maps macro names to their replacement text; `known(name)` tells whether a macro absent
    from `defines` is definitely undefined (True: the name evaluates to 0, `defined()` is false) or
    not decidable (False: the name is UNKNOWN). Expressions evaluate in three-valued logic: a value
    depending on an UNKNOWN operand is UNKNOWN unless a short-circuit decides it (`0 && U` is 0,
    `1 || U` is 1). `__call__(conds)` is False only when some condition is definitely false, so an
    undecidable condition keeps the include (over-approximation)."""

    def __init__(self, defines: dict[str, str], known: Callable[[str], bool]) -> None:
        self.defines = defines
        self.known = known

    def __call__(self, conds: list[str]) -> bool:
        return all(self.evaluate(c) != 0 for c in conds)

    def evaluate(self, expr: str) -> int | None:
        """Value of one `#if` expression, None when UNKNOWN (a parse failure is UNKNOWN)."""
        try:
            tokens = _tokenize(expr)
            self._tokens, self._pos = tokens, 0
            value = self._ternary()
            if self._pos != len(tokens):
                return _UNKNOWN
            return value
        except (_ExprError, RecursionError):
            return _UNKNOWN

    # -- grammar ----------------------------------------------------------------------------------
    def _peek(self) -> tuple[str, str] | None:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _accept(self, op: str) -> bool:
        tok = self._peek()
        if tok and tok == ("op", op):
            self._pos += 1
            return True
        return False

    def _ternary(self) -> int | None:
        cond = self._binary(0)
        if not self._accept("?"):
            return cond
        a = self._ternary()
        if not self._accept(":"):
            raise _ExprError("?:")
        b = self._ternary()
        if cond is _UNKNOWN:
            return a if a is not _UNKNOWN and a == b else _UNKNOWN
        return a if cond else b

    def _binary(self, level: int) -> int | None:
        if level == len(_BINARY_LEVELS):
            return self._unary()
        left = self._binary(level + 1)
        while True:
            tok = self._peek()
            if not tok or tok[0] != "op" or tok[1] not in _BINARY_LEVELS[level]:
                return left
            self._pos += 1
            op = tok[1]
            right = self._binary(level + 1)
            left = self._combine(op, left, right)

    @staticmethod
    def _combine(op: str, a: int | None, b: int | None) -> int | None:
        if op == "&&":
            if a == 0 or b == 0:
                return 0
            return _UNKNOWN if a is _UNKNOWN or b is _UNKNOWN else 1
        if op == "||":
            if (a is not _UNKNOWN and a != 0) or (b is not _UNKNOWN and b != 0):
                return 1
            return _UNKNOWN if a is _UNKNOWN or b is _UNKNOWN else 0
        if a is _UNKNOWN or b is _UNKNOWN:
            return _UNKNOWN
        if op in ("/", "%"):
            if b == 0:
                return _UNKNOWN
            q = abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1)
            return q if op == "/" else a - q * b
        return _BINARY_FUNCS[op](a, b)

    def _unary(self) -> int | None:
        tok = self._peek()
        if tok is None:
            raise _ExprError("end")
        if tok[0] == "op" and tok[1] in ("!", "~", "-", "+"):
            self._pos += 1
            v = self._unary()
            if v is _UNKNOWN:
                return _UNKNOWN
            return {"!": int(v == 0), "~": ~v, "-": -v, "+": v}[tok[1]]
        return self._primary()

    def _primary(self) -> int | None:
        kind, text = self._tokens[self._pos]
        self._pos += 1
        if kind == "num":
            return int(text)
        if kind == "op":
            if text == "(":
                v = self._ternary()
                if not self._accept(")"):
                    raise _ExprError("paren")
                return v
            raise _ExprError(text)
        if text == "defined":
            paren = self._accept("(")
            tok = self._peek()
            if not tok or tok[0] != "id":
                raise _ExprError("defined")
            self._pos += 1
            if paren and not self._accept(")"):
                raise _ExprError("defined(")
            return self._defined(tok[1])
        if self._accept("("):
            return self._call(text)
        return self._identifier(text)

    def _defined(self, name: str) -> int | None:
        if name in self.defines:
            return 1
        return 0 if self.known(name) else _UNKNOWN

    def _call(self, name: str) -> int | None:
        """A function-like macro use; the opening parenthesis is consumed."""
        arg: str | None = None
        depth = 1
        while self._pos < len(self._tokens):
            kind, text = self._tokens[self._pos]
            self._pos += 1
            if kind == "op" and text == "(":
                depth += 1
            elif kind == "op" and text == ")":
                depth -= 1
                if depth == 0:
                    break
            elif kind == "id" and arg is None and depth == 1:
                arg = text
        else:
            raise _ExprError("call")
        if name in ("IS_ENABLED", "IS_BUILTIN", "IS_MODULE", "IS_REACHABLE") and arg:
            builtin = self._defined(arg)
            module = self._defined(arg + "_MODULE")
            if name == "IS_BUILTIN":
                return builtin
            if name == "IS_MODULE":
                return module
            return self._combine("||", builtin, module)
        return _UNKNOWN

    def _identifier(self, name: str) -> int | None:
        if name in self.defines:
            value = self.defines[name].strip()
            m = re.fullmatch(r"(0[xX][0-9a-fA-F]+|\d+)[uUlL]*", value)
            if m:
                return int(m.group(1), 0) if value[:2].lower() == "0x" else int(m.group(1), 10)
            return 1
        return 0 if self.known(name) else _UNKNOWN


class IncludeResolver:
    def __init__(self, layout: ArchLayout, is_file: Callable[[str], bool]) -> None:
        self.layout = layout
        self.is_file = is_file
        self._dir_links = sorted(((p, t) for p, t in layout.links.items() if p.endswith("/")), key=lambda x: -len(x[0]))
        self._file_links = {p: t for p, t in layout.links.items() if not p.endswith("/")}

    def materialize(self, path: str) -> str | None:
        """The tree file a build-time path designates (through links and wrappers), None if none."""
        for _ in range(_MAX_LINK_HOPS):
            if path in self.layout.wrappers:
                path = self.layout.wrappers[path]
                continue
            if path in self._file_links:
                path = self._file_links[path]
                continue
            hit = next(((link, target) for link, target in self._dir_links if path.startswith(link)), None)
            if hit is None:
                break
            path = hit[1] + path[len(hit[0]):]
        return path if self.is_file(path) else None

    def resolve(self, name: str, including_file: str | None = None, quoted: bool = False) -> str | None:
        """Tree file an `#include` of `name` from `including_file` reads, None when not in the tree."""
        name = name.strip()
        if not name or name.startswith("/"):
            return None
        dirs = []
        if quoted and including_file:
            dirs.append(posixpath.dirname(including_file))
        dirs += [d.rstrip("/") for d in self.layout.includes]
        for d in dirs:
            candidate = posixpath.normpath(posixpath.join(d, name)) if d else posixpath.normpath(name)
            if candidate.startswith("../") or candidate == "..":
                continue
            hit = self.materialize(candidate)
            if hit:
                return hit
        return None

    def reachable(self, start: str, directives: Callable[[str], list[tuple[str, bool]] | list[tuple[str, bool, list[str]]] | None],
                  limit: int = 8000,
                  filter_cond: Callable[[list[str]], bool] | None = None,
                  prefetch: Callable[[list[str]], None] | None = None) -> dict[str, str | None]:
        """Files the compilation of `start` reads: {path: the file that included it} (None for `start`
        and the forced includes), in breadth-first order. `directives(path)` lists a file's includes;
        `prefetch(paths)`, when given, receives each breadth-first level before its files' directives are
        asked for (the webapp loads one level's stored directives in one query)."""
        parents: dict[str, str | None] = {}
        level: list[str] = []
        for path in [*self.layout.forced, start]:
            if path and path not in parents and self.is_file(path):
                parents[path] = None
                level.append(path)
        while level and len(parents) < limit:
            if prefetch is not None:
                prefetch(level)
            nxt: list[str] = []
            for current in level:
                if len(parents) >= limit:
                    break
                for item in directives(current) or ():
                    if len(item) == 3:
                        name, quoted, conds = item
                        if filter_cond is not None and not filter_cond(conds):
                            continue
                    else:
                        name, quoted = item[0], item[1]
                    hit = self.resolve(name, current, quoted)
                    if hit and hit not in parents:
                        parents[hit] = current
                        nxt.append(hit)
            level = nxt
        return parents


def include_chain(parents: dict[str, str | None], path: str) -> list[str]:
    """The files from the walk's start down to `path` (empty when `path` was not reached)."""
    if path not in parents:
        return []
    chain = [path]
    while parents.get(chain[-1]) is not None and len(chain) < 64:
        chain.append(parents[chain[-1]])
    return list(reversed(chain))
