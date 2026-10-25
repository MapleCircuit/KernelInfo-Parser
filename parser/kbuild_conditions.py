"""Kbuild build conditions: under which configuration a source file is compiled.

`KbuildConditionResolver` follows what `make ARCH=<arch>` does with a tree: the goal lists of the
top Makefile and `arch/<srcarch>/Makefile` (`core-y`, `drivers-$(CONFIG_X)`, ...) name the top
directories, every directory's Kbuild file (`Kbuild` when present, else `Makefile`) descends into
sub-directories (`obj-$(CONFIG_X) += dir/`) and names its objects (`obj-*`, `lib-*`, `extra-*`,
composite `foo-y`/`foo-objs`/`foo-$(CONFIG_X)` members). Unlike `KbuildParser` (which only records
which symbols a rule mentions), `ifeq`/`ifneq`/`ifdef`/`ifndef`/`else` blocks keep their polarity and
simple variables exported by the arch Makefile (`BITS` on x86) are expanded per alternative.

A file's condition is a tristate expression in JSON form, evaluated against a `.config` by
`webapp/frontend/js/views/kconfig/build_condition.js`:

    "y" / "m" / "n"           constants ("y" built-in, "m" module, "n" not built)
    "?"                       unknown (a rule could not be followed)
    {"sym": "X"}              CONFIG_X as a tristate ("y"/"m"; anything else is "n")
    {"def": "X"}              "y" when CONFIG_X is set (not "n"/unset), else "n"
    {"eq": [a, b]}            "y" when the two expanded make strings are equal; each side is a list
                              of literal strings and {"val": "X"} (CONFIG_X's value, "" when unset/n)
    {"not": e}                boolean negation ("y"/"m" -> "n", "n" -> "y")
    {"and": [...]}            minimum (n < m < y)
    {"or": [...]}             maximum

Unknown values propagate three-valued: `and` with a definite "n" is "n", `or` with a definite "y"
is "y", otherwise "?". Callers only report a file as not compiled when its condition is definitely "n".
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from parser.arch_layout import path_arch

Expr = Any

Y, M, N, UNKNOWN = "y", "m", "n", "?"

# Goal lists of the top Makefile and arch Makefiles; their items are relative to the source root
TOP_GOALS = ("core", "drivers", "net", "init", "libs", "head")
# Lists whose items are compiled objects (or sub-directories) relative to the Kbuild file's directory
DIR_GOALS = ("obj", "lib", "extra")
SOURCE_SUFFIXES = (".c", ".S", ".rs")
# Lists of programs built by their own rules: sources named there are compiled outside obj-*
_HOST_LISTS = {"hostprogs", "hostprogs-always", "userprogs", "userprogs-always", "always", "targets", "subdir"}

_ASSIGN_RE = re.compile(r"^((?:[^:=#\s]|\$\([^)]*\))+?)\s*(:=|::=|\+=|\?=|=)\s*(.*)$")
_LIST_LHS_RE = re.compile(r"^([A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*?)-(y|m|objs|\$\(.+\))$")
_COND_RE = re.compile(r"^(ifeq|ifneq|ifdef|ifndef)\b\s*(.*)$")
_VAR_REF_RE = re.compile(r"\$\(([A-Za-z0-9_]+)\)")
_SUFFIX_CONFIG_RE = re.compile(r"^\$\(CONFIG_([A-Za-z0-9_]+)\)$")
_SUFFIX_SUBST_RE = re.compile(r"^\$\(subst\s+m\s*,\s*y\s*,\s*\$\(CONFIG_([A-Za-z0-9_]+)\)\)$|^\$\(CONFIG_([A-Za-z0-9_]+):m=y\)$")
_MAX_ALTERNATIVES = 4
_DIR_ARCHIVE_RE = re.compile(r"/(?:built-in\.[oa]|lib\.a)$")
_ADDPREFIX_RE = re.compile(r"^\$\(addprefix\s+([^,]+),(.*)\)$", re.S)
_LIST_REF_RE = re.compile(r"^\$\(([A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*?)-(y|m|objs)\)$")


# ---------------------------------------------------------------------------- expressions
def and_(*exprs: Expr) -> Expr:
    """Conjunction with constant folding."""
    terms: list[Expr] = []
    for e in exprs:
        if e == N:
            return N
        if e == Y:
            continue
        if isinstance(e, dict) and "and" in e:
            terms.extend(t for t in e["and"] if t not in terms)
        elif e not in terms:
            terms.append(e)
    if not terms:
        return Y
    return terms[0] if len(terms) == 1 else {"and": terms}


def or_(*exprs: Expr) -> Expr:
    """Disjunction with constant folding."""
    terms: list[Expr] = []
    for e in exprs:
        if e == Y:
            return Y
        if e == N:
            continue
        if isinstance(e, dict) and "or" in e:
            terms.extend(t for t in e["or"] if t not in terms)
        elif e not in terms:
            terms.append(e)
    if not terms:
        return N
    return terms[0] if len(terms) == 1 else {"or": terms}


def not_(e: Expr) -> Expr:
    if e in (Y, M):
        return N
    if e == N:
        return Y
    if e == UNKNOWN:
        return UNKNOWN
    return {"not": e}


def evaluate(expr: Expr, value_of: Callable[[str], str | None]) -> str:
    """Evaluate an expression against `value_of(symbol) -> raw value` (None when unknown). Reference twin
    of `evalBuild()` in webapp/frontend/js/views/kconfig/build_condition.js (used by the tests)."""
    def raw(sym: str) -> str | None:
        v = value_of(sym)
        return None if v is None else ("" if v == "n" else str(v))

    def ev(e: Expr) -> str:
        if isinstance(e, str):
            return e
        if "sym" in e:
            v = value_of(e["sym"])
            return UNKNOWN if v is None else (v if v in (Y, M) else N)
        if "def" in e:
            v = raw(e["def"])
            return UNKNOWN if v is None else (Y if v else N)
        if "eq" in e:
            sides = []
            for side in e["eq"]:
                parts = []
                for p in side:
                    v = raw(p["val"]) if isinstance(p, dict) else p
                    if v is None:
                        return UNKNOWN
                    parts.append(v)
                sides.append("".join(parts).strip())
            return Y if sides[0] == sides[1] else N
        if "not" in e:
            v = ev(e["not"])
            return UNKNOWN if v == UNKNOWN else (N if v in (Y, M) else Y)
        if "and" in e:
            vals = [ev(t) for t in e["and"]]
            if N in vals:
                return N
            if UNKNOWN in vals:
                return UNKNOWN
            return M if M in vals else Y
        if "or" in e:
            vals = [ev(t) for t in e["or"]]
            for v in (Y, M, UNKNOWN):
                if v in vals:
                    return v
            return N
        return UNKNOWN

    return ev(expr)


# ---------------------------------------------------------------------------- one Kbuild file
@dataclass
class Rule:
    """One make line that adds an object or directory to a list."""
    makefile: str
    line: int
    text: str
    cond: Expr

    def as_dict(self) -> dict[str, Any]:
        return {"makefile": self.makefile, "line": self.line, "text": self.text}


@dataclass
class KbuildFile:
    """Objects, sub-directories and composite members named by one Kbuild file (paths from srctree)."""
    path: str
    text: str = ""
    objects: dict[str, list[Rule]] = field(default_factory=dict)
    subdirs: dict[str, list[Rule]] = field(default_factory=dict)
    # composite object path -> [(member object path, rule)]
    composites: dict[str, list[tuple[str, Rule]]] = field(default_factory=dict)
    # list name (`foo` of foo-y / foo-objs / foo-$(CONFIG_X)) -> [(member path, line condition, suffix, gate)]
    lists: dict[str, list[tuple[str, Expr, str, Expr]]] = field(default_factory=dict)
    # Variables assigned here: name -> [(value, condition)] (None when too complex to follow)
    variables: dict[str, list[tuple[str, Expr]] | None] = field(default_factory=dict)
    unresolved_objects: bool = False
    unresolved_dirs: bool = False
    includes_other: bool = False


def _split_words(text: str) -> list[str]:
    """Whitespace-separated words, keeping $(function args) references in one word."""
    words, cur, depth = [], "", 0
    for ch in text:
        if ch == "(" and cur.endswith("$") or ch == "(" and depth:
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        if ch.isspace() and not depth:
            if cur:
                words.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        words.append(cur)
    return words


def _join_lines(content: str) -> list[tuple[int, str]]:
    """Logical lines (backslash continuations joined, comments stripped) with their first line number."""
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 1
    for idx, line in enumerate(content.splitlines(), start=1):
        if not buf:
            start = idx
        text = line.rstrip()
        if text.endswith("\\"):
            buf.append(text[:-1])
            continue
        buf.append(text)
        joined = " ".join(buf)
        buf = []
        hash_at = joined.find("#")
        if hash_at >= 0:
            joined = joined[:hash_at]
        joined = joined.strip()
        if joined:
            out.append((start, joined))
    if buf:
        out.append((start, " ".join(buf).strip()))
    return out


def _has_unknown(e: Expr) -> bool:
    if e == UNKNOWN:
        return True
    if isinstance(e, dict):
        if "not" in e:
            return _has_unknown(e["not"])
        return any(_has_unknown(t) for t in e.get("and", e.get("or", [])))
    return False


class _CondStack:
    """ifeq/ifneq/ifdef/ifndef/else/endif frames, each holding the branch conditions seen so far."""

    def __init__(self, transparent_unknown: bool = False) -> None:
        self.frames: list[list[Expr]] = []
        # Top-level Makefiles branch on make-internal state (mixed-targets, KBUILD_EXTMOD, ...): a frame
        # whose conditions cannot be evaluated applies to none of its branches instead of hiding them
        self.transparent_unknown = transparent_unknown

    def current(self) -> Expr:
        terms = []
        for branches in self.frames:
            if self.transparent_unknown and any(_has_unknown(b) for b in branches):
                continue
            earlier = [not_(b) for b in branches[:-1]]
            terms.append(and_(*earlier, branches[-1]))
        return and_(*terms)

    def push(self, cond: Expr) -> None:
        self.frames.append([cond])

    def else_(self, cond: Expr = Y) -> None:
        if self.frames:
            self.frames[-1].append(cond)

    def pop(self) -> None:
        if self.frames:
            self.frames.pop()


class KbuildFileParser:
    """Parse one Makefile/Kbuild into a `KbuildFile`."""

    def __init__(self, env: dict[str, list[tuple[str, Expr]]], top_level: bool = False) -> None:
        # Known variables (e.g. ARCH, SRCARCH, BITS exported by the arch Makefile) -> alternatives
        self.env = env
        self.top_level = top_level

    # -------------------------------------------------------------- expansion
    def _alternatives(self, name: str, local: dict[str, list[tuple[str, Expr]] | None]) -> list[tuple[str, Expr]] | None:
        if name in local:
            return local[name]
        return self.env.get(name)

    def expand(self, text: str, local: dict[str, list[tuple[str, Expr]] | None]) -> list[tuple[str, Expr]] | None:
        """Every expansion of `text` with its condition; None when a reference cannot be followed."""
        results: list[tuple[str, Expr]] = [("", Y)]
        pos = 0
        for m in _VAR_REF_RE.finditer(text):
            alts = self._alternatives(m.group(1), local)
            if not alts:
                return None
            literal = text[pos:m.start()]
            results = [(s + literal + v, and_(c, vc)) for s, c in results for v, vc in alts]
            results = [(s, c) for s, c in results if c != N]
            if len(results) > _MAX_ALTERNATIVES:
                return None
            pos = m.end()
        tail = text[pos:]
        results = [(s + tail, c) for s, c in results]
        if any("$" in s for s, _ in results):
            return None
        return results

    def _make_string(self, text: str, local: dict[str, list[tuple[str, Expr]] | None]) -> list[Any] | None:
        """A side of an ifeq comparison: literals and {"val": X} for $(CONFIG_X)."""
        parts: list[Any] = []
        pos = 0
        for m in _VAR_REF_RE.finditer(text):
            parts.append(text[pos:m.start()])
            name = m.group(1)
            if name.startswith("CONFIG_"):
                parts.append({"val": name[7:]})
            else:
                alts = self._alternatives(name, local)
                if not alts or len(alts) != 1 or alts[0][1] != Y:
                    return None
                parts.append(alts[0][0])
            pos = m.end()
        tail = text[pos:]
        if "$" in tail:
            return None
        parts.append(tail)
        return [p for p in parts if p != ""]

    @staticmethod
    def _split_ifeq_args(args: str) -> tuple[str, str] | None:
        args = args.strip()
        if args.startswith("("):
            depth, inner = 0, ""
            for i, ch in enumerate(args):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        inner = args[1:i]
                        break
            depth = 0
            for i, ch in enumerate(inner):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                elif ch == "," and depth == 0:
                    return inner[:i].strip(), inner[i + 1:].strip()
            return None
        m = re.match(r"""^(["'])(.*?)\1\s+(["'])(.*?)\3$""", args)
        return (m.group(2), m.group(4)) if m else None

    def condition(self, kind: str, args: str, local: dict[str, list[tuple[str, Expr]] | None]) -> Expr:
        if kind in ("ifdef", "ifndef"):
            name = args.strip()
            if name.startswith("CONFIG_") and re.fullmatch(r"CONFIG_\w+", name):
                cond: Expr = {"def": name[7:]}
            else:
                alts = self._alternatives(name, local)
                if not alts:
                    return UNKNOWN
                cond = or_(*(c for v, c in alts if v.strip()))
            return not_(cond) if kind == "ifndef" else cond
        pair = self._split_ifeq_args(args)
        if not pair:
            return UNKNOWN
        a, b = self._make_string(pair[0], local), self._make_string(pair[1], local)
        if a is None or b is None:
            return UNKNOWN
        if not any(isinstance(p, dict) for p in (*a, *b)):
            cond = Y if "".join(a).strip() == "".join(b).strip() else N
        else:
            cond = {"eq": [a, b]}
        return not_(cond) if kind == "ifneq" else cond

    @staticmethod
    def _list_gate(suffix: str) -> Expr | None:
        if suffix == "y" or suffix == "objs":
            return Y
        if suffix == "m":
            return M
        m = _SUFFIX_CONFIG_RE.match(suffix)
        if m:
            return {"sym": m.group(1)}
        m = _SUFFIX_SUBST_RE.match(suffix)
        if m:
            return {"def": m.group(1) or m.group(2)}
        return None

    # -------------------------------------------------------------- parsing
    def parse(self, content: str, path: str) -> KbuildFile:
        kfile = KbuildFile(path=path, text=content)
        base = "" if self.top_level else os.path.dirname(path)
        conds = _CondStack(transparent_unknown=self.top_level)
        local = kfile.variables
        in_define = False

        def rel(item: str) -> str:
            joined = f"{base}/{item}" if base else item
            return os.path.normpath(joined).replace("\\", "/")

        for line_no, text in _join_lines(content):
            if in_define:
                in_define = not re.match(r"^endef\b", text)
                continue
            if re.match(r"^(?:override\s+|export\s+)?define\b", text):
                in_define = True
                continue
            m = _COND_RE.match(text)
            if m:
                conds.push(self.condition(m.group(1), m.group(2), local))
                continue
            if text == "else" or text.startswith("else ") or text.startswith("else\t"):
                rest = text[4:].strip()
                m = _COND_RE.match(rest)
                conds.else_(self.condition(m.group(1), m.group(2), local) if m else Y)
                continue
            if text == "endif" or text.startswith("endif "):
                conds.pop()
                continue
            if "$(wildcard" in text or "$(shell" in text:
                # Object lists computed from the tree or a command cannot be followed
                kfile.unresolved_objects = True
            if re.match(r"^-?include\b|^sinclude\b", text):
                if "scripts/" not in text and "Kbuild.include" not in text:
                    kfile.includes_other = True
                continue
            m = _ASSIGN_RE.match(text)
            if not m:
                continue
            lhs, op, rhs = m.group(1).strip(), m.group(2), m.group(3).strip()
            gate_cond = conds.current()
            if lhs in _HOST_LISTS or lhs.split("-$(")[0] in _HOST_LISTS:
                kfile.unresolved_objects = True
            lm = _LIST_LHS_RE.match(lhs)
            if lm:
                if lm.group(1) in _HOST_LISTS:
                    # Programs and sub-directories make builds outside obj-* (sources stay "unknown")
                    kfile.unresolved_objects = True
                    if lm.group(1) == "subdir":
                        gate = self._list_gate(lm.group(2)) or UNKNOWN
                        for word in _split_words(rhs):
                            if "$" not in word:
                                kfile.subdirs.setdefault(rel(word.rstrip("/")), []).append(
                                    Rule(kfile.path, line_no, text[:200], and_(gate_cond, gate)))
                    continue
                self._list_assignment(kfile, lm.group(1), lm.group(2), rhs, gate_cond, line_no, text[:200], rel)
                continue
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", lhs):
                self._variable(local, lhs, op, rhs, gate_cond)
        return kfile

    def _variable(self, local: dict[str, list[tuple[str, Expr]] | None], name: str, op: str, rhs: str, cond: Expr) -> None:
        if op == "+=" or "$(" in rhs or local.get(name, []) is None:
            local[name] = None
            return
        alts = local.setdefault(name, [])
        if cond == Y and op != "?=":
            alts.clear()
        alts.append((rhs, cond))
        if len(alts) > _MAX_ALTERNATIVES:
            local[name] = None

    def _list_assignment(self, kfile: KbuildFile, name: str, suffix: str, rhs: str, cond: Expr,
                         line_no: int, text: str, rel: Callable[[str], str]) -> None:
        if name not in (*TOP_GOALS, *DIR_GOALS):
            # Composite object `name.o` made of these members (or a list referenced as $(name-y))
            gate = self._list_gate(suffix)
            kind = suffix if suffix in ("y", "m", "objs") else "cfg"
            composite = rel(f"{name}.o")
            for item, item_cond, absolute in self._items(rhs, kfile):
                if item is None:
                    continue
                if item.endswith(".o"):
                    path = item if absolute else rel(item)
                    kfile.composites.setdefault(composite, []).append(
                        (path, Rule(kfile.path, line_no, text, and_(cond, gate or UNKNOWN, item_cond))))
                    kfile.lists.setdefault(name, []).append((path, and_(cond, item_cond), kind, gate or UNKNOWN))
            return
        if name in TOP_GOALS and not self.top_level:
            return
        gate = self._list_gate(suffix)
        if gate is None:
            alts = self.expand(suffix, kfile.variables)
            gate = or_(*(and_(c, Y if v == "y" else M if v == "m" else N) for v, c in alts)) if alts else UNKNOWN
        is_lib = name in ("lib", "libs")
        for item, item_cond, absolute in self._items(rhs, kfile):
            if item is None:
                kfile.unresolved_objects = True
                kfile.unresolved_dirs = True
                continue
            rule = Rule(kfile.path, line_no, text, and_(cond, gate, item_cond))
            if absolute:
                kfile.objects.setdefault(item, []).append(rule)
            elif item.endswith("/"):
                kfile.subdirs.setdefault(rel(item.rstrip("/")), []).append(rule)
            elif _DIR_ARCHIVE_RE.search(item):
                # Older Makefiles link a sub-directory through its archive: obj-y += selinux/built-in.o
                kfile.subdirs.setdefault(rel(_DIR_ARCHIVE_RE.sub("", item)), []).append(rule)
            elif item.endswith(".o") or name == "extra":
                kfile.objects.setdefault(rel(item), []).append(rule)
            elif is_lib and item.endswith(".a"):
                continue

    def _items(self, rhs: str, kfile: KbuildFile) -> list[tuple[str | None, Expr, bool]]:
        """(word, condition, already a srctree path) per item; None words could not be expanded."""
        out: list[tuple[str | None, Expr, bool]] = []
        for word in _split_words(rhs):
            if "$" not in word:
                out.append((word, Y, False))
                continue
            pre = _ADDPREFIX_RE.match(word)
            if pre and "$" not in pre.group(1):
                # $(addprefix libxfs/, xfs_ag.o xfs_alloc.o ...)
                out.extend((None if w is None else (w if a else pre.group(1).strip() + w), c, a)
                           for w, c, a in self._items(pre.group(2), kfile))
                continue
            ref = _LIST_REF_RE.match(word)
            if ref and ref.group(1) in kfile.lists:
                # $(proc-y): the members of proc-$(CONFIG_PROC_FS) when CONFIG_PROC_FS=y
                out.extend((path, and_(line_cond, _list_selects(kind, gate, ref.group(2))), True)
                           for path, line_cond, kind, gate in kfile.lists[ref.group(1)])
                continue
            alts = self.expand(word, kfile.variables)
            if alts is None:
                out.append((None, UNKNOWN, False))
            else:
                out.extend((v, c, False) for v, c in alts)
        return out


def _list_selects(kind: str, gate: Expr, wanted: str) -> Expr:
    """Whether a member added to `<name>-<suffix>` (kind/gate) is part of `$(<name>-<wanted>)`."""
    if wanted == "objs" or kind == "objs":
        return Y if wanted == kind else N
    if kind in ("y", "m"):
        return Y if kind == wanted else N
    if isinstance(gate, dict) and "sym" in gate:
        return {"eq": [[{"val": gate["sym"]}], [wanted]]}
    if isinstance(gate, dict) and "def" in gate:
        return gate if wanted == "y" else N
    return UNKNOWN


# ---------------------------------------------------------------------------- architecture trees
def own_arch_dirs(srcarch: str) -> set[str]:
    """Fallback for revisions without a build layout (parser/arch_layout.py): the architecture trees
    ARCH=<srcarch> is assumed to use (ARCH=um also the host's x86 / x86_64 trees)."""
    return {srcarch} | ({"x86", "x86_64"} if srcarch == "um" else set())


def is_other_arch_path(path: str, srcarch: str, trees: Iterable[str] | None = None) -> bool:
    """Whether `path` lies in the tree of an architecture the build does not use. `trees` are the
    architecture trees of the ARCH's build layout (ArchLayout.arch_trees()); without them
    `own_arch_dirs(srcarch)` is assumed."""
    arch = path_arch(path)
    return arch is not None and arch not in (set(trees) if trees is not None else own_arch_dirs(srcarch))


# ---------------------------------------------------------------------------- whole tree
class KbuildConditionResolver:
    """Build conditions of the files of one revision for one ARCH.

    Args:
        read: path -> file content at the revision (None when absent).
        exists: path -> whether the revision has that file.
        arch: ARCH value (e.g. "x86_64").
        srcarch: its source directory under arch/ (e.g. "x86").
        included_sources: basenames of .c/.S files some other file #includes (never reported as unbuilt).
        arch_trees: architecture trees the ARCH's build layout uses (ArchLayout.arch_trees()); files of
            other architecture trees are "other_arch". Defaults to own_arch_dirs(srcarch).
    """

    def __init__(self, read: Callable[[str], str | None], exists: Callable[[str], bool], arch: str, srcarch: str,
                 included_sources: set[str] | None = None, arch_trees: Iterable[str] | None = None) -> None:
        self.read = read
        self.exists = exists
        self.arch = arch
        self.srcarch = srcarch
        self.included_sources = included_sources or set()
        self.arch_trees = set(arch_trees) if arch_trees is not None else own_arch_dirs(srcarch)
        self._files: dict[str, KbuildFile | None] = {}
        self._dir_conds: dict[str, tuple[Expr, list[Rule]]] = {}
        self._top: KbuildFile | None = None
        self._top_text = ""
        self.env = self._arch_env()

    # -------------------------------------------------------------- top level
    def _arch_env(self) -> dict[str, list[tuple[str, Expr]]]:
        env: dict[str, list[tuple[str, Expr]]] = {"ARCH": [(self.arch, Y)], "SRCARCH": [(self.srcarch, Y)]}
        arch_mk = self.read(f"arch/{self.srcarch}/Makefile") or ""
        exported = set()
        for _, text in _join_lines(arch_mk):
            m = re.match(r"^export\s+(.+)$", text)
            if m:
                exported.update(w for w in m.group(1).split() if re.fullmatch(r"[A-Za-z_]\w*", w))
        parsed = KbuildFileParser(env, top_level=True).parse(arch_mk, f"arch/{self.srcarch}/Makefile")
        for name in exported:
            alts = parsed.variables.get(name)
            if alts:
                env[name] = alts
        return env

    def _top_file(self) -> KbuildFile:
        if self._top is None:
            root = self.read("Makefile") or ""
            arch_path = f"arch/{self.srcarch}/Makefile"
            arch_mk = self.read(arch_path) or ""
            self._top_text = root + "\n" + arch_mk
            parser = KbuildFileParser(self.env, top_level=True)
            top = parser.parse(root, "Makefile")
            sources = [parser.parse(arch_mk, arch_path)]
            # Newer kernels descend from the root Kbuild file (obj-y += init/ usr/ arch/$(SRCARCH)/ ...)
            root_kbuild = self.read("Kbuild") if self.exists("Kbuild") else None
            if root_kbuild:
                self._top_text += "\n" + root_kbuild
                sources.append(KbuildFileParser(self.env).parse(root_kbuild, "Kbuild"))
            for src in sources:
                for k, rules in src.objects.items():
                    top.objects.setdefault(k, []).extend(rules)
                for k, rules in src.subdirs.items():
                    top.subdirs.setdefault(k, []).extend(rules)
            self._top = top
        return self._top

    def kbuild_file(self, directory: str) -> KbuildFile | None:
        """Parsed Kbuild file of a directory (`Kbuild` takes precedence over `Makefile`)."""
        if directory not in self._files:
            parsed = None
            for name in ("Kbuild", "Makefile"):
                path = f"{directory}/{name}" if directory else name
                if directory and self.exists(path):
                    content = self.read(path)
                    if content is not None:
                        parsed = KbuildFileParser(self.env).parse(content, path)
                        break
            self._files[directory] = parsed
        return self._files[directory]

    def is_other_arch(self, path: str) -> bool:
        return is_other_arch_path(path, self.srcarch, self.arch_trees)

    def _mentioned_at_top(self, directory: str) -> bool:
        self._top_file()
        return bool(re.search(rf"(?<![\w/]){re.escape(directory)}(?![\w-])", self._top_text))

    # -------------------------------------------------------------- directories
    def dir_condition(self, directory: str) -> tuple[Expr, list[Rule]]:
        """Condition under which make descends into `directory`, and the rules that lead there."""
        directory = directory.strip("/")
        if directory in self._dir_conds:
            return self._dir_conds[directory]
        self._dir_conds[directory] = (UNKNOWN, [])  # cycle guard
        result: tuple[Expr, list[Rule]]
        top = self._top_file()
        if not directory:
            result = (Y, [])
        elif self.is_other_arch(directory + "/"):
            result = (N, [])
        elif directory in top.subdirs:
            rules = top.subdirs[directory]
            result = (or_(*(r.cond for r in rules)), rules[:1])
        else:
            parent = os.path.dirname(directory)
            pfile = self.kbuild_file(parent) if parent else None
            rules = (pfile.subdirs.get(directory) if pfile else None) or []
            if rules and parent:
                pcond, prules = self.dir_condition(parent)
                result = (and_(or_(*(r.cond for r in rules)), pcond), [*rules[:1], *prules])
            elif not parent or pfile is None or pfile.unresolved_dirs or pfile.includes_other:
                result = (UNKNOWN, [])
            elif (self._mentioned_at_top(directory)
                  or re.search(rf"(?<![\w-]){re.escape(os.path.basename(directory))}(?![\w-])", pfile.text)
                  or not self.exists(f"{directory}/Makefile") and not self.exists(f"{directory}/Kbuild")):
                # Built by its own make target (e.g. arch/x86/boot), or not a Kbuild directory at all
                result = (UNKNOWN, [])
            else:
                pcond, prules = self.dir_condition(parent)
                result = (N if pcond != UNKNOWN else UNKNOWN, prules)
        self._dir_conds[directory] = result
        return result

    # -------------------------------------------------------------- files
    def file_condition(self, path: str) -> dict[str, Any]:
        """Build condition of one file: {"status", "expr", "rules", "reason"}.

        status: "other_arch" (never built for this ARCH), "rule" (expr says when), "no_rule" (a fully
        parsed Kbuild file of a built directory does not name the object: never built), "unknown".
        """
        path = path.strip("/")
        if self.is_other_arch(path):
            return {"status": "other_arch", "expr": N, "rules": [],
                    "reason": f"{'/'.join(path.split('/')[:2])}/ is not built for ARCH={self.arch}"}
        stem, ext = os.path.splitext(path)
        if path.endswith(".lds.S"):
            stem, ext = path[: -len(".lds.S")] + ".lds", ".S"
        if ext not in SOURCE_SUFFIXES:
            return {"status": "unknown", "expr": UNKNOWN, "rules": [], "reason": "not a Kbuild source"}
        directory = os.path.dirname(path)
        obj = stem if stem.endswith(".lds") else stem + ".o"
        alternatives: list[Expr] = []
        used: list[Rule] = []

        def add(rules: list[Rule], member_cond: Expr, mk_dir: str) -> None:
            dcond, drules = self.dir_condition(mk_dir)
            for r in rules:
                alternatives.append(and_(r.cond, member_cond, dcond))
            used.extend([*rules[:1], *drules])

        top = self._top_file()
        if obj in top.objects:
            add(top.objects[obj], Y, "")
        own = self.kbuild_file(directory) if directory else None
        d = directory
        for _ in range(3):  # the object's own Kbuild file, or an ancestor naming `sub/obj.o`
            kfile = self.kbuild_file(d) if d else None
            if kfile is not None:
                if obj in kfile.objects:
                    add(kfile.objects[obj], Y, d)
                for composite, members in kfile.composites.items():
                    for member, mrule in members:
                        if member == obj and composite in kfile.objects:
                            add(kfile.objects[composite], mrule.cond, d)
                            used.append(mrule)
            if not d:
                break
            d = os.path.dirname(d)

        if alternatives:
            return {"status": "rule", "expr": or_(*alternatives), "rules": [r.as_dict() for r in _dedup(used)], "reason": ""}
        base_obj = os.path.basename(obj)
        if (own is None or own.unresolved_objects or own.includes_other
                or os.path.basename(path) in self.included_sources
                # Named by a custom rule or list (e.g. vdso objects, targets): built some other way
                or re.search(rf"(?<![\w-]){re.escape(base_obj)}(?![\w-])", own.text)
                # Built by a top-level rule (e.g. asm-offsets.c from the root Kbuild, as asm-offsets.s)
                or re.search(rf"(?<![\w-]){re.escape(os.path.basename(stem))}\.[A-Za-z]\b", self._top_text)):
            return {"status": "unknown", "expr": UNKNOWN, "rules": [], "reason": "no Kbuild rule could be followed"}
        dcond, drules = self.dir_condition(directory)
        if dcond == UNKNOWN:
            return {"status": "unknown", "expr": UNKNOWN, "rules": [], "reason": "directory build rule unknown"}
        return {"status": "no_rule", "expr": N, "rules": [r.as_dict() for r in drules],
                "reason": f"{own.path} does not build {base_obj}"}


def _dedup(rules: list[Rule]) -> list[Rule]:
    seen: set[tuple[str, int]] = set()
    out = []
    for r in rules:
        if (r.makefile, r.line) not in seen:
            seen.add((r.makefile, r.line))
            out.append(r)
    return out
