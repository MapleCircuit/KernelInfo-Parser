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
Every `#include` line counts, whatever `#if` block it sits in, so the walk over-approximates what one
configuration reads; callers that know the configuration filter the result further.
"""
from __future__ import annotations

import posixpath
import re
from collections import deque
from collections.abc import Callable

from parser.arch_layout import ArchLayout

INCLUDE_RE = re.compile(r'^[ \t]*#[ \t]*include(?:_next)?[ \t]*([<"])([^>"\n]+)[>"]', re.MULTILINE)
_MAX_LINK_HOPS = 8


def include_directives(content: str) -> list[tuple[str, bool]]:
    """(name, quoted) of every `#include` / `#include_next` with a literal name, in file order."""
    return [(m.group(2).strip(), m.group(1) == '"') for m in INCLUDE_RE.finditer(content or "")]


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

    def reachable(self, start: str, directives: Callable[[str], list[tuple[str, bool]] | None],
                  limit: int = 8000) -> dict[str, str | None]:
        """Files the compilation of `start` reads: {path: the file that included it} (None for `start`
        and the forced includes), in breadth-first order. `directives(path)` lists a file's includes."""
        parents: dict[str, str | None] = {}
        queue: deque[str] = deque()
        for path in [*self.layout.forced, start]:
            if path and path not in parents and self.is_file(path):
                parents[path] = None
                queue.append(path)
        while queue and len(parents) < limit:
            current = queue.popleft()
            for name, quoted in directives(current) or ():
                hit = self.resolve(name, current, quoted)
                if hit and hit not in parents:
                    parents[hit] = current
                    queue.append(hit)
        return parents


def include_chain(parents: dict[str, str | None], path: str) -> list[str]:
    """The files from the walk's start down to `path` (empty when `path` was not reached)."""
    if path not in parents:
        return []
    chain = [path]
    while parents.get(chain[-1]) is not None and len(chain) < 64:
        chain.append(parents[chain[-1]])
    return list(reversed(chain))
