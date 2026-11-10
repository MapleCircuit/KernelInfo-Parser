"""parser/arch_layout.py - Where each architecture's sources and headers are, read from the Makefiles.

For one revision and one ARCH value, `ArchLayout` records what `make ARCH=<arch>` uses, as evaluated
by `parser/make_eval.py` from the revision's own Makefiles (top `Makefile`, the `include`d arch
Makefile, `scripts/Makefile.lib`, `scripts/Makefile.asm-generic`):

- `sources`: directories of architecture code it builds: `arch/<srcarch>/` and the other `arch/`
  directories its goal lists name (`core-y += arch/x86/um/` for ARCH=um).
- `includes`: the `-I` directories of `c_flags` in search order (`arch/x86/include/`,
  `arch/x86/include/generated/`, `include/`, ...; `include/` alone in 2.6.12, where `<asm/...>` goes
  through the `include/asm` symlink).
- `links`: symlinks the build creates (`include/asm` -> `include/asm-x86_64/` in 2.6.x,
  `include/asm-um/arch` -> `include/asm-x86_64/` for ARCH=um), from the `ln` commands of the rules.
- `wrappers`: headers `scripts/Makefile.asm-generic` generates for `generic-y` / `mandatory-y`
  (`arch/x86/include/generated/asm/early_ioremap.h`), each mapped to the header it includes
  (`include/asm-generic/early_ioremap.h`).
- `forced`: files `-include`d into every compilation (`include/linux/kconfig.h`, ...).

The only assumption is the build host (`uname -m`, x86_64), which decides ARCH=um's SUBARCH.

`arch_trees()` are the architecture trees an ARCH uses (`path_arch()` of its directories): x86_64
uses `x86_64` (arch/x86_64/, include/asm-x86_64/) at v2.6.12 and `x86` at v3.0; ARCH=um also uses
`x86`; m68knommu's headers come from `m68k` (hdr-arch) in 2.6.29-2.6.37.
"""
from __future__ import annotations

import hashlib
import logging
import posixpath
import shlex
from collections.abc import Callable, Iterable
from typing import Any
from dataclasses import dataclass, field

from parser.kconfig_ast.kconfig_model import arch_aliases
from parser.make_eval import SRC_ROOT, UNKNOWN, MakeEvaluator, Tree, tree_path

logger = logging.getLogger(__name__)

ROLE_SOURCE, ROLE_INCLUDE, ROLE_LINK, ROLE_WRAPPER, ROLE_FORCED = 1, 2, 3, 4, 5
ROLE_NAMES = {ROLE_SOURCE: "source", ROLE_INCLUDE: "include", ROLE_LINK: "link", ROLE_WRAPPER: "wrapper", ROLE_FORCED: "forced"}
HOST_MACHINE = "x86_64"

_GOAL_LISTS = ("head-y", "init-y", "core-y", "libs-y", "drivers-y", "net-y", "virt-y")
_FLAG_VARS = ("LINUXINCLUDE", "CPPFLAGS", "KBUILD_CPPFLAGS", "CFLAGS", "KBUILD_CFLAGS")
_SHELL_SEPARATORS = ("&&", "||", ";", "|", "\n")


def path_arch(path: str) -> str | None:
    """Architecture tree holding `path`: `arch/<a>/...`, or `include/asm-<a>/...` (the arch headers
    before they moved to arch/<a>/include/asm/, 2.6.x); None for generic code and include/asm-generic/."""
    parts = (path or "").split("/")
    if len(parts) >= 2:
        if parts[0] == "arch" and parts[1] and parts[1] != "Kconfig" and "." not in parts[1]:
            return parts[1]
        if parts[0] == "include" and parts[1].startswith("asm-") and parts[1] != "asm-generic":
            return parts[1][len("asm-"):]
    return None


def layout_digest(rows: Iterable[tuple[int, int, str, str]]) -> bytes:
    """Identity of a layout (m_arch_layout.layout_hash): first 16 bytes of the SHA-256 of its sorted
    (role, priority, path, target) rows, so equal layouts of different versions are stored once."""
    h = hashlib.sha256()
    for role, priority, path, target in sorted(set(rows)):
        h.update(f"{int(role)}\t{int(priority)}\t{path}\t{target or ''}\n".encode("utf-8", "surrogatepass"))
    return h.digest()[:16]


@dataclass(frozen=True)
class ArchRoot:
    """One row of a layout (m_arch_layout_entry, core/DBLayout.py)."""
    arch: str
    role: int
    priority: int
    path: str
    target: str = ""


@dataclass
class ArchLayout:
    arch: str
    srcarch: str
    sources: list[str] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)
    wrappers: dict[str, str] = field(default_factory=dict)
    forced: list[str] = field(default_factory=list)

    def rows(self) -> list[ArchRoot]:
        out = [ArchRoot(self.arch, ROLE_SOURCE, i, p) for i, p in enumerate(self.sources)]
        out += [ArchRoot(self.arch, ROLE_INCLUDE, i, p) for i, p in enumerate(self.includes)]
        out += [ArchRoot(self.arch, ROLE_LINK, 0, p, t) for p, t in sorted(self.links.items())]
        out += [ArchRoot(self.arch, ROLE_WRAPPER, 0, p, t) for p, t in sorted(self.wrappers.items())]
        out += [ArchRoot(self.arch, ROLE_FORCED, i, p) for i, p in enumerate(self.forced)]
        return out

    @classmethod
    def from_rows(cls, arch: str, rows: Iterable[tuple[int, int, str, str]]) -> ArchLayout:
        """Layout of (role, priority, path, target) rows; srcarch is the first source directory's."""
        layout = cls(arch, arch)
        ordered: dict[int, list[tuple[int, str, str]]] = {}
        for role, priority, path, target in rows:
            ordered.setdefault(int(role), []).append((int(priority), path, target or ""))
        for role, items in ordered.items():
            items.sort()
            if role == ROLE_SOURCE:
                layout.sources = [p for _, p, _ in items]
            elif role == ROLE_INCLUDE:
                layout.includes = [p for _, p, _ in items]
            elif role == ROLE_LINK:
                layout.links = {p: t for _, p, t in items}
            elif role == ROLE_WRAPPER:
                layout.wrappers = {p: t for _, p, t in items}
            elif role == ROLE_FORCED:
                layout.forced = [p for _, p, _ in items]
        if layout.sources:
            layout.srcarch = path_arch(layout.sources[0]) or arch
        return layout

    def arch_trees(self) -> set[str]:
        """Architecture trees (path_arch names) this ARCH builds or takes headers from."""
        paths = [*self.sources, *self.includes, *self.links.values(), *self.wrappers]
        return {a for a in (path_arch(p) for p in paths) if a} | {self.srcarch}

    def generic_headers(self) -> set[str]:
        """include/asm-generic/ headers this ARCH uses through generated wrappers."""
        return set(self.wrappers.values())


# ---------------------------------------------------------------------------------------------- extraction
def _dir(path: str) -> str:
    return path if path.endswith("/") else path + "/"


def _flag_tokens(text: str) -> list[str]:
    return [w for w in text.split() if UNKNOWN not in w]


def _include_flags(ev: MakeEvaluator) -> tuple[list[str], list[str]]:
    """(-I directories, -include files) of the C compilation flags, in command-line order."""
    tokens = _flag_tokens(ev.value("c_flags")) if ev.defined("c_flags") else []
    if not tokens:
        for name in _FLAG_VARS:
            tokens += _flag_tokens(ev.value(name))
    dirs: list[str] = []
    after: list[str] = []
    forced: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if tok in ("-I", "-idirafter", "-include"):
            value, i = nxt, i + 2
        elif tok.startswith("-I"):
            tok, value, i = "-I", tok[2:], i + 1
        else:
            i += 1
            continue
        rel = tree_path(value)
        if rel is None:
            continue
        if tok == "-include":
            if rel not in forced:
                forced.append(rel)
        else:
            (after if tok == "-idirafter" else dirs).append(_dir(rel) if rel else "")
    ordered: list[str] = []
    for d in dirs + after:
        if d not in ordered:
            ordered.append(d)
    return ordered, forced


def _sources(ev: MakeEvaluator, srcarch: str, tree: Tree) -> list[str]:
    out = [f"arch/{srcarch}/"]
    for name in _GOAL_LISTS:
        for w in ev.words(name):
            rel = tree_path(w)
            if rel is None or not rel.startswith("arch/"):
                continue
            d = _dir(rel) if w.endswith("/") else _dir(posixpath.dirname(rel))
            if tree.is_dir(d) and not any(d.startswith(o) for o in out):
                out.append(d)
    # Ensure sub-architecture directories (e.g. arch/arm/mach-*, arch/arm/plat-*) are properly traversed
    for prefix in (f"arch/{srcarch}/mach-", f"arch/{srcarch}/plat-"):
        for d in sorted(tree.glob(f"{prefix}*")):
            if tree.is_dir(d):
                d_dir = _dir(d)
                if d_dir not in out:
                    out.append(d_dir)
    return out


def _shell_segments(command: str) -> list[str]:
    segments = [command]
    for sep in _SHELL_SEPARATORS:
        segments = [part for seg in segments for part in seg.split(sep)]
    out = []
    for seg in segments:
        seg = seg.strip().lstrip("@-+").strip()
        for kw in ("then ", "else ", "do "):
            if seg.startswith(kw):
                seg = seg[len(kw):].strip()
        if seg:
            out.append(seg)
    return out


def _links(ev: MakeEvaluator, tree: Tree) -> dict[str, str]:
    """Symlinks into the tree created by `ln` commands of the rules (link path -> target path)."""
    links: dict[str, str] = {}
    for rule in ev.rules:
        if not any("ln " in line for line in rule.recipe):
            continue
        for target in rule.targets:
            cwd = ""
            for command in ev.recipe_commands(rule, target):
                for seg in _shell_segments(command):
                    try:
                        tokens = shlex.split(seg)
                    except ValueError:
                        tokens = seg.split()
                    if not tokens:
                        continue
                    if tokens[0] == "cd" and len(tokens) == 2:
                        cwd = tree_path(tokens[1], cwd) or ""
                        continue
                    if tokens[0] != "ln":
                        continue
                    args = [t for t in tokens[1:] if not t.startswith("-")]
                    if len(args) not in (1, 2):
                        continue
                    src, dst = (args[0], ".") if len(args) == 1 else args
                    link = tree_path(dst, cwd)
                    if link is None:
                        continue
                    if tree.is_dir(link) or link == "":
                        link = posixpath.join(link, posixpath.basename(src))
                    # Links the compilation can see (headers and sources), created inside the tree
                    if (not link.startswith(("include/", "arch/")) or tree.exists(link)
                            or not tree.is_dir(posixpath.dirname(link))):
                        continue
                    anchored = src.startswith((SRC_ROOT, "/"))
                    dest = tree_path(src) if anchored else tree_path(src, posixpath.dirname(link))
                    if dest is None or not tree.exists(dest):
                        continue
                    if tree.is_dir(dest):
                        links[_dir(link)] = _dir(dest)
                    else:
                        links[link] = dest
    return links


def _sub_makes(ev: MakeEvaluator, target: str) -> list[tuple[str, dict[str, str]]]:
    """(makefile, command-line variables) of the `make -f` commands in the recipe of `target`."""
    out = []
    for rule in ev.rules_for(target):
        for command in ev.recipe_commands(rule, target):
            for seg in _shell_segments(command):
                try:
                    tokens = shlex.split(seg)
                except ValueError:
                    continue
                if not tokens or posixpath.basename(tokens[0]) != "make":
                    continue
                makefile, assigns, i = None, {}, 1
                while i < len(tokens):
                    tok = tokens[i]
                    if tok == "-f" and i + 1 < len(tokens):
                        makefile, i = tokens[i + 1], i + 2
                        continue
                    if "=" in tok and not tok.startswith("-"):
                        k, v = tok.split("=", 1)
                        assigns[k] = v
                    i += 1
                rel = tree_path(makefile) if makefile else None
                if rel:
                    out.append((rel, assigns))
    return out


def _wrappers(ev: MakeEvaluator, tree: Tree, read: Callable[[str], str | None], includes: list[str],
              parsed: dict) -> dict[str, str]:
    """Headers generated by scripts/Makefile.asm-generic, mapped to the asm-generic header they include."""
    out: dict[str, str] = {}
    env = ev.exported_values()
    for target in ("asm-generic", "uapi-asm-generic"):
        for makefile, assigns in _sub_makes(ev, target):
            obj = tree_path(assigns.get("obj", ""))
            if not obj:
                continue
            sub = MakeEvaluator(tree, read, {**ev.cmdline, **assigns}, env, ev.host_machine, parsed)
            if not sub.include(makefile):
                continue
            for rule in sub.rules_for("all"):
                for w in rule.prereqs.split():
                    rel = tree_path(w)
                    if rel is None or not rel.endswith(".h") or not rel.startswith(obj + "/"):
                        continue
                    name = "asm-generic/" + rel[len(obj) + 1:]
                    dest = next((d + name for d in includes if tree.is_file(d + name)), None)
                    if dest:
                        out[rel] = dest
    return out


def layout_for(arch: str, tree: Tree, read: Callable[[str], str | None], host_machine: str = HOST_MACHINE,
               parsed: dict | None = None) -> ArchLayout | None:
    """Layout of one ARCH value, or None when the tree does not build it."""
    parsed = parsed if parsed is not None else {}
    ev = MakeEvaluator(tree, read, {"ARCH": arch}, None, host_machine, parsed)
    if not ev.include("Makefile"):
        return None
    srcarch = ev.value("SRCARCH").strip() or arch
    if UNKNOWN in srcarch or not tree.is_file(f"arch/{srcarch}/Makefile"):
        return None
    ev.include("scripts/Makefile.lib")
    includes, forced = _include_flags(ev)
    includes = [d for d in includes if tree.is_dir(d) or "generated" in d.split("/")]
    layout = ArchLayout(arch, srcarch, sources=_sources(ev, srcarch, tree), includes=includes,
                        forced=[f for f in forced if tree.is_file(f)])
    layout.links = _links(ev, tree)
    layout.wrappers = _wrappers(ev, tree, read, includes, parsed)
    return layout


def arch_values(tree: Tree, top_makefile: str) -> list[str]:
    """ARCH values a revision builds: its arch/ directories and the aliases of the top Makefile."""
    dirs = sorted(d.split("/")[1] for d in tree.dirs if d.count("/") == 1 and d.startswith("arch/"))
    values = [d for d in dirs if tree.is_file(f"arch/{d}/Makefile")]
    for alias, srcarch in sorted(arch_aliases(top_makefile).items()):
        if alias not in values and srcarch in values:
            values.append(alias)
    return values


def compute_layouts(files: Iterable[str], read: Callable[[str], str | None], arches: Iterable[str] | None = None,
                    host_machine: str = HOST_MACHINE) -> dict[str, ArchLayout]:
    """Layouts of a revision (all its ARCH values unless `arches` is given). An ARCH whose Makefiles
    cannot be evaluated is logged and left out."""
    tree = files if isinstance(files, Tree) else Tree(files)
    parsed: dict = {}
    top = read("Makefile") if tree.is_file("Makefile") else None
    if top is None:
        return {}
    out: dict[str, ArchLayout] = {}
    for arch in (list(arches) if arches is not None else arch_values(tree, top)):
        try:
            layout = layout_for(arch, tree, read, host_machine, parsed)
        except RecursionError:
            layout = None
            logger.warning("ARCH=%s: Makefile evaluation recursed too deeply", arch)
        except Exception as exc:  # one architecture's Makefiles must not stop the others
            layout = None
            logger.warning("ARCH=%s: Makefile evaluation failed: %s", arch, exc)
        if layout is not None:
            out[arch] = layout
    return out


# --------------------------------------------------------------------------- reuse across versions

_VERSION_LINE = ("VERSION", "PATCHLEVEL", "SUBLEVEL", "EXTRAVERSION", "NAME")


def input_key_of(blob_of: Callable[[str], Any], read: Callable[[str], str | None]) -> Callable[[str], Any]:
    """The key a trace records for a file: its blob, except the top Makefile, whose release lines
    (`VERSION =`, `PATCHLEVEL =`, `SUBLEVEL =`, `EXTRAVERSION =`, `NAME =`) change with every release and
    name no path of a layout: the digest of its other lines."""
    def key(path: str) -> Any:
        if path != "Makefile":
            return blob_of(path)
        text = read(path)
        if text is None:
            return None
        kept = [ln for ln in text.splitlines() if ln.split("=", 1)[0].strip() not in _VERSION_LINE]
        return "top:" + hashlib.sha1("\n".join(kept).encode("utf-8", "replace")).hexdigest()
    return key


def layout_rows(layout: ArchLayout) -> list[tuple[int, int, str, str]]:
    """(role, priority, path, target) rows of a layout as m_arch_layout_entry stores them (paths cut to 255)."""
    return sorted({(r.role, r.priority, r.path[:255], r.target[:255]) for r in layout.rows()})


class InputTrace:
    """Everything one ARCH's evaluation read from a revision, so a later revision can tell whether the same
    evaluation would give the same layout: every file used (its blob, None when absent) and every tree
    query with its answer (`is_file`, `is_dir`, `exists`, `glob`). Passed to `layout_for()` as the tree;
    `read()` is its file reader. Listing the whole tree (`files`, `dirs`) makes the trace unreplayable."""

    def __init__(self, tree: Tree, read: Callable[[str], str | None], blob_of: Callable[[str], Any]) -> None:
        self._tree = tree
        self._read = read
        self._blob_of = blob_of
        self.entries: dict[tuple[str, str], Any] = {}
        self.replayable = True

    def note_file(self, path: str) -> None:
        self.entries.setdefault(("file", path), self._blob_of(path))

    def read(self, path: str) -> str | None:
        self.note_file(path)
        return self._read(path)

    def _ask(self, kind: str, arg: str, value: Any) -> Any:
        self.entries.setdefault((kind, arg), value)
        return value

    def is_file(self, path: str) -> bool:
        return self._ask("is_file", path, self._tree.is_file(path))

    def is_dir(self, path: str) -> bool:
        return self._ask("is_dir", path, self._tree.is_dir(path))

    def exists(self, path: str) -> bool:
        return self._ask("exists", path, self._tree.exists(path))

    def glob(self, pattern: str) -> list[str]:
        hits = self._tree.glob(pattern)
        self._ask("glob", pattern, tuple(hits))
        return hits

    @property
    def files(self) -> set[str]:
        self.replayable = False
        return self._tree.files

    @property
    def dirs(self) -> set[str]:
        self.replayable = False
        return self._tree.dirs

    def replays(self, tree: Tree, blob_of: Callable[[str], Any]) -> bool:
        """True when every recorded input has the same answer in `tree` (with its blobs `blob_of`)."""
        if not self.replayable:
            return False
        for (kind, arg), value in self.entries.items():
            if kind == "file":
                now = blob_of(arg)
            elif kind == "glob":
                now = tuple(tree.glob(arg))
            else:
                now = getattr(tree, kind)(arg)
            if now != value:
                return False
        return True


class _TracedParsed(dict):
    """The parsed-makefile cache shared by the ARCH evaluations: a hit is a file use of the current trace."""

    def __init__(self) -> None:
        super().__init__()
        self.trace: InputTrace | None = None

    def __contains__(self, key: object) -> bool:
        if self.trace is not None and isinstance(key, str):
            self.trace.note_file(key)
        return super().__contains__(key)

    def __getitem__(self, key: str) -> Any:
        if self.trace is not None:
            self.trace.note_file(key)
        return super().__getitem__(key)


def compute_layout_rows(tree: Tree, read: Callable[[str], str | None], blob_of: Callable[[str], Any],
                        memo: dict[str, tuple[InputTrace, list | None]] | None = None,
                        host_machine: str = HOST_MACHINE) -> tuple[dict[str, list], dict[str, tuple[InputTrace, list | None]], int]:
    """`compute_layouts()` as rows ({arch: layout_rows}), reusing the rows of `memo` (the previous
    revision's {arch: (trace, rows or None)}) for every ARCH whose trace replays against this tree.
    `blob_of(path)` is the blob of a file of the tree (None when absent). Returns (rows by ARCH, the memo
    for the next revision, number of ARCH values reused)."""
    top = read("Makefile") if tree.is_file("Makefile") else None
    if top is None:
        return {}, {}, 0
    blob_of = input_key_of(blob_of, read)
    memo = memo or {}
    out: dict[str, list] = {}
    new_memo: dict[str, tuple[InputTrace, list | None]] = {}
    parsed = _TracedParsed()
    reused = 0
    for arch in arch_values(tree, top):
        prev = memo.get(arch)
        if prev is not None and prev[0].replays(tree, blob_of):
            reused += 1
            new_memo[arch] = prev
            if prev[1] is not None:
                out[arch] = prev[1]
            continue
        trace = InputTrace(tree, read, blob_of)
        parsed.trace = trace
        try:
            layout = layout_for(arch, trace, trace.read, host_machine, parsed)
        except RecursionError:
            layout = None
            logger.warning("ARCH=%s: Makefile evaluation recursed too deeply", arch)
        except Exception as exc:  # one architecture's Makefiles must not stop the others
            layout = None
            logger.warning("ARCH=%s: Makefile evaluation failed: %s", arch, exc)
        finally:
            parsed.trace = None
        rows = layout_rows(layout) if layout is not None else None
        new_memo[arch] = (trace, rows)
        if rows is not None:
            out[arch] = rows
    return out, new_memo, reused
