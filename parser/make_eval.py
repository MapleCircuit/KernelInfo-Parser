"""parser/make_eval.py - A small GNU make evaluator for reading the kernel's build layout.

`MakeEvaluator` reads makefiles the way `make` does before running anything: logical lines
(backslash continuations), conditionals (`ifeq`/`ifneq`/`ifdef`/`ifndef`/`else`/`endif`), variable
assignments (`=`, `:=`, `::=`, `?=`, `+=`, `!=`, `override`, `export`, command-line precedence),
`define`/`endef`, `include`/`-include`, and rules with their recipes. Expansion implements variable
and substitution references, automatic variables and the text functions the kernel Makefiles use
(`if`, `filter`, `patsubst`, `foreach`, `call`, `wildcard`, `origin`, ...). Recipes are kept
unexpanded; `expand()` with automatic variables expands them later, as make does when it runs them.

It evaluates one in-tree `make ARCH=<arch>` with no goal and no `.config`:
- `$(CURDIR)` is `SRC_ROOT` (`tree_path()` turns such paths back into repository paths), so
  `srctree`/`objtree` follow the Makefile's own in-tree logic; `MAKECMDGOALS` is empty.
- CONFIG_* variables are undefined (no `include/config/auto.conf`), so config-dependent settings
  take their "not configured" branch.
- `$(shell uname -m | sed -e s/.../.../ ...)` is computed for the host machine `host_machine`
  (x86_64 by default), the only fact a make run takes from outside the tree. Other `$(shell)` calls,
  compiler checks and unknown functions expand to the `UNKNOWN` marker: words containing it are
  unknown, and a conditional testing an unknown value applies both branches.
"""
from __future__ import annotations

import fnmatch
import posixpath
import re
import shlex
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

UNKNOWN = "\x00"
SRC_ROOT = "/@src"

_MAX_DEPTH = 120
_MAX_INCLUDE_DEPTH = 10
_COND_RE = re.compile(r"^(ifeq|ifneq|ifdef|ifndef)\b\s*(.*)$", re.DOTALL)
_FUNC_RE = re.compile(r"^([a-z][a-z-]*)[ \t]")
_FUNCS = {
    "subst": 3, "patsubst": 3, "strip": 1, "findstring": 2, "filter": 2, "filter-out": 2, "sort": 1,
    "word": 2, "wordlist": 3, "words": 1, "firstword": 1, "lastword": 1, "dir": 1, "notdir": 1,
    "suffix": 1, "basename": 1, "addsuffix": 2, "addprefix": 2, "join": 2, "wildcard": 1,
    "realpath": 1, "abspath": 1, "if": 3, "or": 0, "and": 0, "foreach": 3, "call": 0, "value": 1,
    "origin": 1, "flavor": 1, "shell": 1, "error": 1, "warning": 1, "info": 1, "eval": 1, "file": 2,
    "let": 3, "intcmp": 0, "guile": 1,
}


def tree_path(path: str, cwd: str = "") -> str | None:
    """Repository-relative form of a path make uses (None when unknown or outside the tree)."""
    if not path or UNKNOWN in path or "$" in path:
        return None
    if path == SRC_ROOT or path.startswith(SRC_ROOT + "/"):
        rel = path[len(SRC_ROOT) + 1:]
    elif path.startswith("/"):
        return None
    else:
        rel = posixpath.join(cwd, path) if cwd else path
    rel = posixpath.normpath(rel) if rel else "."
    if rel == ".":
        return ""
    if rel == ".." or rel.startswith("../"):
        return None
    return rel


class Tree:
    """The files of one revision, for `$(wildcard)` and existence checks."""

    def __init__(self, files: Iterable[str]) -> None:
        self.files = set(files)
        self.dirs = {""}
        for f in self.files:
            d = posixpath.dirname(f)
            while d not in self.dirs:
                self.dirs.add(d)
                d = posixpath.dirname(d)
        self._glob_cache: dict[str, list[str]] = {}

    def is_file(self, path: str) -> bool:
        return path in self.files

    def is_dir(self, path: str) -> bool:
        return path.rstrip("/") in self.dirs

    def exists(self, path: str) -> bool:
        return self.is_file(path) or self.is_dir(path)

    def glob(self, pattern: str) -> list[str]:
        if pattern not in self._glob_cache:
            if not any(c in pattern for c in "*?["):
                hits = [pattern] if self.exists(pattern) else []
            else:
                prefix = re.split(r"[*?\[]", pattern, maxsplit=1)[0]
                base = prefix if prefix.endswith("/") else posixpath.dirname(prefix) + "/"
                base = "" if base == "/" else base
                depth = pattern.count("/")
                hits = sorted(
                    p for p in (*self.files, *self.dirs)
                    if p.startswith(base) and p.count("/") == depth and fnmatch.fnmatchcase(p, pattern)
                )
            self._glob_cache[pattern] = hits
        return self._glob_cache[pattern]


@dataclass
class Rule:
    targets: list[str]
    prereqs: str
    recipe: list[str] = field(default_factory=list)
    makefile: str = ""


@dataclass
class _Line:
    text: str
    recipe: bool
    lineno: int


def logical_lines(content: str) -> list[_Line]:
    """Makefile lines with backslash continuations joined; comments stripped outside recipes."""
    out: list[_Line] = []
    raw = content.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(raw):
        start = i
        line = raw[i]
        while line.endswith("\\") and not line.endswith("\\\\") and i + 1 < len(raw):
            i += 1
            line = line[:-1].rstrip() + " " + raw[i].lstrip()
        i += 1
        recipe = line.startswith("\t")
        text = line[1:] if recipe else _strip_comment(line)
        out.append(_Line(text, recipe, start + 1))
    return out


def _strip_comment(line: str) -> str:
    out = []
    i = 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line) and line[i + 1] == "#":
            out.append("#")
            i += 2
            continue
        if c == "#":
            break
        out.append(c)
        i += 1
    return "".join(out).rstrip()


def _matching(text: str, start: int) -> int:
    """Index of the parenthesis closing text[start] ('(' or '{'), -1 if unbalanced."""
    opener = text[start]
    closer = ")" if opener == "(" else "}"
    depth = 0
    for j in range(start, len(text)):
        c = text[j]
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return j
    return -1


def _split_args(text: str, count: int) -> list[str]:
    """Split function arguments at top-level commas (at most `count` parts when count > 0)."""
    parts: list[str] = []
    depth = 0
    cur = []
    for c in text:
        if c in "({":
            depth += 1
        elif c in ")}":
            depth -= 1
        if c == "," and depth == 0 and (count <= 0 or len(parts) < count - 1):
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(c)
    parts.append("".join(cur))
    return parts


def _pattern_match(pattern: str, word: str) -> str | None:
    """Stem of `word` for a make pattern with at most one '%' (None when it does not match)."""
    if "%" not in pattern:
        return "" if pattern == word else None
    pre, post = pattern.split("%", 1)
    if len(word) >= len(pre) + len(post) and word.startswith(pre) and word.endswith(post):
        return word[len(pre):len(word) - len(post)]
    return None


def _patsubst(pattern: str, replacement: str, words: list[str]) -> list[str]:
    out = []
    for w in words:
        stem = _pattern_match(pattern, w)
        if stem is None:
            out.append(w)
        else:
            out.append(replacement.replace("%", stem, 1) if "%" in replacement else replacement)
    return out


class MakeEvaluator:
    """Variables and rules of a make run (see the module docstring for what is assumed)."""

    def __init__(self, tree: Tree, read: Callable[[str], str | None], cmdline: dict[str, str] | None = None,
                 env: dict[str, str] | None = None, host_machine: str = "x86_64",
                 parsed: dict[str, list[_Line]] | None = None) -> None:
        self.tree = tree
        self.read = read
        self.host_machine = host_machine
        self.vars: dict[str, tuple[str, str]] = {}
        self.origins: dict[str, str] = {}
        self.exported: set[str] = set()
        self.rules: list[Rule] = []
        self._parsed = parsed if parsed is not None else {}
        self._locals: list[dict[str, str]] = []
        self._depth = 0
        self.cmdline = dict(cmdline or {})
        defaults = {"MAKE": "make", "CC": "cc", "AR": "ar", "SHELL": "/bin/sh", "MAKE_VERSION": "4.3",
                    "MAKEFLAGS": "", "MAKECMDGOALS": "", "CURDIR": SRC_ROOT, "MAKEFILE_LIST": ""}
        for name, value in defaults.items():
            self._set(name, "simple", value, "default")
        for name, value in (env or {}).items():
            self._set(name, "simple", value, "environment")
            self.exported.add(name)
        for name, value in self.cmdline.items():
            self._set(name, "simple", value, "command line")

    # ------------------------------------------------------------------ variables
    def _set(self, name: str, flavor: str, value: str, origin: str) -> None:
        self.vars[name] = (flavor, value)
        self.origins[name] = origin

    def defined(self, name: str) -> bool:
        return name in self.vars

    def value(self, name: str) -> str:
        """Expanded value of a variable ('' when undefined)."""
        return self._var(name, None)

    def words(self, name: str) -> list[str]:
        """Known words of a variable's value (words containing UNKNOWN are dropped)."""
        return [w for w in self.value(name).split() if UNKNOWN not in w]

    def exported_values(self) -> dict[str, str]:
        """Expanded values of the exported variables (the environment of a sub-make)."""
        return {name: self.value(name) for name in self.exported if name in self.vars and name not in self.cmdline}

    def _var(self, name: str, auto: dict[str, str] | None) -> str:
        for scope in reversed(self._locals):
            if name in scope:
                return scope[name]
        if auto is not None and name in auto:
            return auto[name]
        if len(name) == 2 and name[1] in "DF" and auto is not None and name[0] in auto:
            v = auto[name[0]]
            return (posixpath.dirname(v) or ".") if name[1] == "D" else posixpath.basename(v)
        hit = self.vars.get(name)
        if hit is None:
            return ""
        flavor, value = hit
        if flavor == "simple":
            return value
        return self.expand(value, auto)

    # ------------------------------------------------------------------ expansion
    def expand(self, text: str, auto: dict[str, str] | None = None) -> str:
        if "$" not in text:
            return text
        self._depth += 1
        try:
            if self._depth > _MAX_DEPTH:
                return UNKNOWN
            out: list[str] = []
            i, n = 0, len(text)
            while i < n:
                j = text.find("$", i)
                if j < 0:
                    out.append(text[i:])
                    break
                out.append(text[i:j])
                if j + 1 >= n:
                    out.append("$")
                    break
                nxt = text[j + 1]
                if nxt == "$":
                    out.append("$")
                    i = j + 2
                elif nxt in "({":
                    k = _matching(text, j + 1)
                    if k < 0:
                        out.append(UNKNOWN)
                        break
                    out.append(self._reference(text[j + 2:k], auto))
                    i = k + 1
                else:
                    out.append(self._var(nxt, auto))
                    i = j + 2
            return "".join(out)
        finally:
            self._depth -= 1

    def _reference(self, inner: str, auto: dict[str, str] | None) -> str:
        m = _FUNC_RE.match(inner)
        if m and m.group(1) in _FUNCS:
            return self._function(m.group(1), inner[m.end():], auto)
        name = self.expand(inner, auto)
        # Substitution reference $(var:a=b) / $(var:%.o=%.c)
        if ":" in name and "=" in name.split(":", 1)[1]:
            var, sub = name.split(":", 1)
            frm, to = sub.split("=", 1)
            words = self._var(var.strip(), auto).split()
            if "%" not in frm:
                frm, to = "%" + frm, "%" + to
            return " ".join(_patsubst(frm, to, words))
        if UNKNOWN in name:
            return UNKNOWN
        return self._var(name.strip(), auto)

    def _function(self, name: str, args_text: str, auto: dict[str, str] | None) -> str:
        args = _split_args(args_text, _FUNCS[name])
        def x(k: int) -> str:
            return self.expand(args[k], auto) if k < len(args) else ""

        if name == "if":
            cond = x(0).strip()
            if UNKNOWN in cond:
                return UNKNOWN
            return x(1) if cond else x(2)
        if name in ("or", "and"):
            last = ""
            for k in range(len(args)):
                v = x(k).strip()
                if UNKNOWN in v:
                    return UNKNOWN
                if name == "or" and v:
                    return v
                if name == "and" and not v:
                    return ""
                last = v
            return "" if name == "or" else last
        if name == "foreach":
            var = x(0).strip()
            out = []
            for w in x(1).split():
                self._locals.append({var: w})
                try:
                    out.append(self.expand(args[2], auto) if len(args) > 2 else "")
                finally:
                    self._locals.pop()
            return " ".join(o for o in out if o.strip())
        if name == "call":
            fname = x(0).strip()
            if UNKNOWN in fname:
                return UNKNOWN
            scope = {"0": fname, **{str(k): x(k) for k in range(1, len(args))}}
            hit = self.vars.get(fname)
            if hit is None:
                return ""
            self._locals.append(scope)
            try:
                return self.expand(hit[1], auto) if hit[0] == "recursive" else hit[1]
            finally:
                self._locals.pop()
        if name == "value":
            hit = self.vars.get(x(0).strip())
            return hit[1] if hit else ""
        if name == "origin":
            var = x(0).strip()
            if any(var in s for s in self._locals) or (auto and var in auto):
                return "automatic"
            return self.origins.get(var, "undefined")
        if name == "flavor":
            hit = self.vars.get(x(0).strip())
            return "undefined" if hit is None else hit[0]
        if name == "shell":
            return self._shell(x(0))
        if name in ("error", "warning", "info", "eval"):
            return ""
        if name in ("file", "let", "intcmp", "guile"):
            return UNKNOWN
        if name == "subst":
            frm, to, text = x(0), x(1), x(2)
            return text.replace(frm, to) if frm else text
        if name == "patsubst":
            return " ".join(_patsubst(x(0).strip(), x(1).strip(), x(2).split()))
        if name == "strip":
            return " ".join(x(0).split())
        if name == "findstring":
            return x(0) if x(0) in x(1) else ""
        if name in ("filter", "filter-out"):
            pats = [p for p in x(0).split() if UNKNOWN not in p]
            keep = name == "filter"
            return " ".join(w for w in x(1).split() if any(_pattern_match(p, w) is not None for p in pats) == keep)
        if name == "sort":
            return " ".join(sorted(set(x(0).split())))
        if name == "word":
            try:
                n = int(x(0).strip())
            except ValueError:
                return UNKNOWN
            words = x(1).split()
            return words[n - 1] if 0 < n <= len(words) else ""
        if name == "wordlist":
            try:
                s, e = int(x(0).strip()), int(x(1).strip())
            except ValueError:
                return UNKNOWN
            return " ".join(x(2).split()[s - 1:e])
        if name == "words":
            return str(len(x(0).split()))
        if name == "firstword":
            words = x(0).split()
            return words[0] if words else ""
        if name == "lastword":
            words = x(0).split()
            return words[-1] if words else ""
        if name == "dir":
            return " ".join((posixpath.dirname(w) + "/") if "/" in w else "./" for w in x(0).split())
        if name == "notdir":
            return " ".join(posixpath.basename(w) for w in x(0).split())
        if name == "suffix":
            return " ".join(posixpath.splitext(w)[1] for w in x(0).split() if posixpath.splitext(w)[1])
        if name == "basename":
            return " ".join(posixpath.splitext(w)[0] for w in x(0).split())
        if name == "addsuffix":
            return " ".join(w + x(0) for w in x(1).split())
        if name == "addprefix":
            return " ".join(x(0) + w for w in x(1).split())
        if name == "join":
            a, b = x(0).split(), x(1).split()
            return " ".join((a[k] if k < len(a) else "") + (b[k] if k < len(b) else "") for k in range(max(len(a), len(b))))
        if name == "wildcard":
            out = []
            for w in x(0).split():
                rel = tree_path(w)
                if rel is None:
                    continue
                for hit in self.tree.glob(rel):
                    out.append(w if not any(c in w for c in "*?[") else
                               (SRC_ROOT + "/" + hit if w.startswith(SRC_ROOT) else hit))
            return " ".join(out)
        if name in ("realpath", "abspath"):
            out = []
            for w in x(0).split():
                rel = tree_path(w)
                if rel is None:
                    return UNKNOWN
                if name == "realpath" and rel and not self.tree.exists(rel):
                    continue
                out.append(SRC_ROOT + ("/" + rel if rel else ""))
            return " ".join(out)
        return UNKNOWN

    def _shell(self, cmd: str) -> str:
        """`uname -m [| sed -e s/a/b/ ...]` for the host machine; other commands are unknown."""
        if UNKNOWN in cmd:
            return UNKNOWN
        m = re.fullmatch(r"\s*uname\s+-m\s*(?:\|\s*sed\s+(.*?))?\s*", cmd, re.DOTALL)
        if not m:
            return UNKNOWN
        value = self.host_machine
        if m.group(1):
            try:
                tokens = shlex.split(m.group(1))
            except ValueError:
                return UNKNOWN
            exprs = [tokens[k + 1] for k in range(len(tokens) - 1) if tokens[k] == "-e"] or tokens[:1]
            for expr in exprs:
                sm = re.fullmatch(r"s(.)(.*?)\1(.*?)\1(g?)", expr)
                if not sm:
                    return UNKNOWN
                try:
                    repl = sm.group(3)
                    value = re.sub(sm.group(2), lambda _m, r=repl: r, value, count=0 if sm.group(4) else 1)
                except re.error:
                    return UNKNOWN
        return value

    # ------------------------------------------------------------------ reading makefiles
    def include(self, path: str, depth: int = 0) -> bool:
        """Read one makefile of the tree (a repository path); False when it does not exist."""
        if depth > _MAX_INCLUDE_DEPTH or not self.tree.is_file(path):
            return False
        if path not in self._parsed:
            content = self.read(path)
            if content is None:
                return False
            self._parsed[path] = logical_lines(content)
        flavor, mlist = self.vars.get("MAKEFILE_LIST", ("simple", ""))
        self.vars["MAKEFILE_LIST"] = (flavor, f"{mlist} {path}".strip())
        self._run(self._parsed[path], path, depth)
        return True

    def _run(self, lines: list[_Line], makefile: str, depth: int) -> None:
        # Conditional frames: [state of the current branch (True / False / None = unknown),
        #                      a branch was definitely taken, a branch was unknown]
        frames: list[list] = []
        rule: Rule | None = None
        define: tuple[str, str, list[str], bool, bool] | None = None
        define_depth = 0

        def active() -> bool:
            return all(f[0] is not False for f in frames)

        for line in lines:
            text = line.text
            if define is not None:
                stripped = text.strip()
                if re.match(r"^(export\s+|override\s+)*define\b", stripped):
                    define_depth += 1
                elif re.match(r"^endef\b", stripped):
                    if define_depth == 0:
                        name, op, body, is_export, is_override = define
                        if active():
                            self._assign(name, op if op else "=", "\n".join(body), is_override, raw=True)
                            if is_export:
                                self.exported.add(name)
                        define = None
                        continue
                    define_depth -= 1
                define[2].append(("\t" if line.recipe else "") + text)
                continue
            if line.recipe and rule is not None:
                if active():
                    rule.recipe.append(text)
                continue
            stripped = text.strip()
            if not stripped:
                continue
            dm = re.match(r"^((?:export|override)\s+)*define\s+(\S+?)\s*(=|:=|::=|\+=|\?=)?\s*$", stripped)
            if dm:
                prefix = dm.group(1) or ""
                define = (dm.group(2), dm.group(3) or "", [], "export" in prefix, "override" in prefix)
                define_depth = 0
                rule = None
                continue
            m = _COND_RE.match(stripped)
            if m:
                if not active():
                    frames.append([False, True, False])
                else:
                    c = self._condition(m.group(1), m.group(2))
                    frames.append([c, c is True, c is None])
                continue
            if stripped == "else" or stripped.startswith("else "):
                if frames:
                    f = frames[-1]
                    parent_active = all(fr[0] is not False for fr in frames[:-1])
                    rest = stripped[4:].strip()
                    if not parent_active or f[1]:
                        f[0] = False
                    else:
                        mm = _COND_RE.match(rest) if rest else None
                        c = self._condition(mm.group(1), mm.group(2)) if mm else True
                        f[0] = (None if c is not False else False) if f[2] else c
                        f[1] = f[1] or c is True
                        f[2] = f[2] or c is None
                continue
            if re.match(r"^endif\b", stripped):
                if frames:
                    frames.pop()
                continue
            if not active():
                rule = None
                continue
            im = re.match(r"^(-include|sinclude|include)\s+(.*)$", stripped)
            if im:
                rule = None
                for w in self.expand(im.group(2)).split():
                    rel = tree_path(w)
                    if rel is not None:
                        self.include(rel, depth + 1)
                continue
            if re.match(r"^(vpath|unexport)\b", stripped):
                continue
            em = re.match(r"^export\s+([^=:]+)$", stripped)
            if em:
                self.exported.update(n for n in self.expand(em.group(1)).split() if UNKNOWN not in n)
                continue
            parsed = self._parse_assignment(stripped)
            if parsed:
                rule = None
                name_text, op, value, is_override, is_export = parsed
                name = self.expand(name_text).strip()
                if name and UNKNOWN not in name and " " not in name:
                    self._assign(name, op, value, is_override)
                    if is_export:
                        self.exported.add(name)
                continue
            colon = self._rule_colon(stripped)
            if colon >= 0:
                targets = [t for t in self.expand(stripped[:colon]).split() if UNKNOWN not in t]
                rest = stripped[colon + 1:].lstrip(":")
                recipe_inline = None
                if ";" in rest:
                    rest, recipe_inline = rest.split(";", 1)
                # Target-specific variable assignments (`foo: VAR := x`) are not rules
                if self._parse_assignment(rest.strip()):
                    rule = None
                    continue
                rule = Rule(targets, self.expand(rest), [], makefile)
                if recipe_inline is not None and recipe_inline.strip():
                    rule.recipe.append(recipe_inline.strip())
                self.rules.append(rule)
                continue
            # Bare function calls ($(error ...), $(shell mkdir ...), $(eval ...)): no layout effect

    @staticmethod
    def _parse_assignment(text: str) -> tuple[str, str, str, bool, bool] | None:
        """(name, operator, value, override, export) of a variable assignment, else None (rules...)."""
        is_override = is_export = False
        while True:
            m = re.match(r"^(override|export|private)\s+", text)
            if not m:
                break
            is_override |= m.group(1) == "override"
            is_export |= m.group(1) == "export"
            text = text[m.end():]
        depth = 0
        for k, c in enumerate(text):
            if c in "({":
                depth += 1
            elif c in ")}":
                depth -= 1
            elif depth == 0 and c == ":":
                for op in (":::=", "::=", ":="):
                    if text.startswith(op, k):
                        name = text[:k].strip()
                        return (name, op, text[k + len(op):], is_override, is_export) if name else None
                return None  # a rule
            elif depth == 0 and c == "=":
                op_start = k - 1 if k > 0 and text[k - 1] in "+?!" else k
                name = text[:op_start].strip()
                return (name, text[op_start:k + 1], text[k + 1:], is_override, is_export) if name else None
        return None

    @staticmethod
    def _rule_colon(text: str) -> int:
        depth = 0
        for k, c in enumerate(text):
            if c in "({":
                depth += 1
            elif c in ")}":
                depth -= 1
            elif c == ":" and depth == 0:
                return k
        return -1

    def _assign(self, name: str, op: str, value: str, is_override: bool, raw: bool = False) -> None:
        if self.origins.get(name) == "command line" and not is_override:
            return
        value = value if raw else value.strip()
        hit = self.vars.get(name)
        if op in (":=", "::=", ":::="):
            self._set(name, "simple", self.expand(value), "file")
        elif op == "?=":
            if hit is None:
                self._set(name, "recursive", value, "file")
        elif op == "+=":
            if hit is None:
                self._set(name, "recursive", value, "file")
            elif hit[0] == "simple":
                self._set(name, "simple", f"{hit[1]} {self.expand(value)}".strip(), self.origins.get(name, "file"))
            else:
                self._set(name, "recursive", f"{hit[1]} {value}".strip(), self.origins.get(name, "file"))
        elif op == "!=":
            self._set(name, "simple", self._shell(self.expand(value)), "file")
        else:
            self._set(name, "recursive", value, "file")

    def _condition(self, kind: str, args: str) -> bool | None:
        """True / False, or None when the compared values are unknown."""
        if kind in ("ifdef", "ifndef"):
            # Only whether the variable has a non-empty value (make does not expand it)
            name = self.expand(args).strip()
            if UNKNOWN in name:
                return None
            hit = self.vars.get(name)
            isdef = bool(hit and hit[1].strip())
            return isdef if kind == "ifdef" else not isdef
        args = args.strip()
        if args.startswith("("):
            end = _matching(args, 0)
            if end < 0:
                return None
            parts = _split_args(args[1:end], 2)
            if len(parts) != 2:
                return None
            a, b = parts
        else:
            m = re.match(r"""^(["'])(.*?)\1\s+(["'])(.*?)\3\s*$""", args)
            if not m:
                return None
            a, b = m.group(2), m.group(4)
        va, vb = self.expand(a).strip(), self.expand(b).strip()
        if UNKNOWN in va or UNKNOWN in vb:
            return None
        equal = va == vb
        return equal if kind == "ifeq" else not equal

    # ------------------------------------------------------------------ results
    def rules_for(self, target: str) -> list[Rule]:
        return [r for r in self.rules if target in r.targets]

    def recipe_commands(self, rule: Rule, target: str) -> list[str]:
        """The rule's recipe for one of its targets, expanded with $@ / $< / $^."""
        prereqs = [p for p in rule.prereqs.split() if p != "|"]
        auto = {"@": target, "<": prereqs[0] if prereqs else "", "^": " ".join(prereqs), "*": UNKNOWN}
        out = []
        for line in rule.recipe:
            expanded = self.expand(line, auto)
            out.extend(expanded.split("\n"))
        return out
