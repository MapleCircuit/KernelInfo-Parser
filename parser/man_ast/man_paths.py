"""parser/man_ast/man_paths.py - man-pages repository path helpers (pure, no git, no DB).

A page file is `<dir>/<name>.<S>` where `<dir>` is `man/man<S>` (new layout) or `man<S>` (old layout) and the
section `<S>` is a digit plus an optional lowercase suffix equal to the directory suffix (`2`, `2const`,
`3type`, `3p`, ...). `.so` alias targets are relative to the man root (`man/` in the new layout, the repository
root in the old one).
"""
from __future__ import annotations

import posixpath
import re

PAGE_RE = re.compile(r"^(?:man/)?man(?P<dsec>[0-9][a-z]*)/(?P<name>[^/]+)\.(?P<sec>[0-9][a-z]*)$")

# Search order for a bare name (no section given); suffixed sections follow their base section.
SECTION_ORDER: list[str] = [
    "2", "2type", "2const",
    "3", "3type", "3const", "3head", "3attr", "3p",
    "1", "1p",
    "8",
    "5",
    "4",
    "7",
    "6",
    "9",
    "0", "0p",
]
_RANK = {s: 2 * i for i, s in enumerate(SECTION_ORDER)}


def page_of_path(path: str) -> tuple[str, str] | None:
    """`(name, section)` for `man/manS/name.S` or `manS/name.S`, else None."""
    if not path:
        return None
    m = PAGE_RE.match(path)
    if not m or m.group("dsec") != m.group("sec"):
        return None
    return m.group("name"), m.group("sec")


def candidate_paths(name: str, section: str) -> list[str]:
    """Repository paths a page `name(section)` may live at, new layout first."""
    return [f"man/man{section}/{name}.{section}", f"man{section}/{name}.{section}"]


def resolve_so(so_target: str, page_path: str) -> str:
    """Repository path of a `.so` target written in the page at `page_path`.

    The target is relative to the man root: `man/` is prefixed when the page lives under `man/`. A bare file
    name (no directory) resolves next to the page.
    """
    target = (so_target or "").strip().strip('"')
    while target.startswith("./"):
        target = target[2:]
    target = target.lstrip("/")
    page_dir = posixpath.dirname(page_path or "")
    if "/" not in target:
        return posixpath.join(page_dir, target) if page_dir else target
    if (page_path or "").startswith("man/") and not target.startswith("man/"):
        return "man/" + target
    return target


def section_rank(section: str) -> int:
    """Sort key of a section in `SECTION_ORDER` (unknown sections sort after the listed ones)."""
    if section in _RANK:
        return _RANK[section]
    base = re.match(r"\d+", section or "")
    if base and base.group(0) in _RANK:
        return _RANK[base.group(0)] + 1
    return 2 * len(SECTION_ORDER)
