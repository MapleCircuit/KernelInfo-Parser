"""parser/kbuild_symbols.py - Kbuild rules with the CONFIG_ symbols that govern them (pure).

What `KbuildParser.parse_makefile_content()` finds in one Makefile/Kbuild file, as the rows of
`m_kbuild_symbol` (core/API.md section 4, row 61): per binding one row for the symbol of its rule
(`obj-$(CONFIG_X)`; `y`/`m` for `obj-y`/`obj-m`) and one per CONFIG_ symbol of the enclosing
`ifeq`/`ifdef` blocks. Shared by the parser (Step 6.6, `--backfill-kbuild`) and the webapp's Kconfig
usage (`KconfigService.kbuild_usage`), so the database and the Git path build the same rules.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Iterable

VIA_NONE = 0  # marker row of a file without any binding: the pair was processed
VIA_RULE = 1
VIA_CONDITION = 2
MARKER_IDX = 65535
KIND_OBJECT = 1
KIND_COMPOSITE = 2
KIND_DIRECTORY = 3
KIND_MAKEFILE = 4  # include of another Makefile (FileRefType.Makefile)
KIND_NAMES = {KIND_OBJECT: "object", KIND_COMPOSITE: "composite", KIND_DIRECTORY: "directory", KIND_MAKEFILE: "object"}
COMPILE_MODES = {1: "built-in", 2: "module", 3: "conditional"}
SYMBOL_LEN = 64
TARGET_OBJ_LEN = 128
PATH_LEN = 255
RULE_LEN = 255


def is_kbuild_file(path: str) -> bool:
    """Makefile and Kbuild files, the ones the webapp's `git grep -- *Makefile* *Kbuild*` reads
    (`Makefile`, `Kbuild`, `Makefile.lib`, `Kbuild.include`, ...)."""
    base = os.path.basename(path)
    return "Makefile" in base or "Kbuild" in base


def target_candidates(path: str) -> list[str]:
    """The paths a target may be: a `.c` object can be built from a `.S` or `.rs` source."""
    if path.endswith(".c"):
        return [path, path[:-2] + ".S", path[:-2] + ".rs"]
    return [path]


def binding_kind(b: Any) -> int:
    if getattr(b, "is_directory", False):
        return KIND_DIRECTORY
    if getattr(b, "is_composite", False):
        return KIND_COMPOSITE
    if int(getattr(b, "ref_type", 3)) == 4:
        return KIND_MAKEFILE
    return KIND_OBJECT


def binding_rows(bindings: Iterable[Any], fnid_of: Callable[[str], int | None]) -> list[tuple]:
    """(idx, line_no, symbol, via, compile_mode, kind, target_obj, target_path, target_fnid, rule) rows of
    one file's bindings; target_fnid is the fnid of the target or of its .S/.rs variant, else 0. A file
    without bindings gets one marker row (idx MARKER_IDX, via VIA_NONE), so every processed (path, blob)
    pair has a row and readers can tell "nothing to store" from "not processed"."""
    rows: list[tuple] = []
    for idx, b in enumerate(bindings):
        target = b.source_file_rel or ""
        target_fnid = 0
        for cand in target_candidates(target):
            fnid = fnid_of(cand)
            if fnid:
                target_fnid = int(fnid)
                target = cand
                break
        common = (int(b.compile_mode or 3), binding_kind(b), (b.target_obj or "")[:TARGET_OBJ_LEN], target[:PATH_LEN],
                  target_fnid, (b.details or "")[:RULE_LEN])
        line = int(b.source_line or 1)
        seen: set[tuple[str, int]] = set()
        for symbol, via in [(b.symbol_name or "", VIA_RULE)] + [(c, VIA_CONDITION) for c in (b.cond_symbols or ())]:
            key = (symbol[:SYMBOL_LEN], via)
            if key in seen:
                continue
            seen.add(key)
            rows.append((idx, line, key[0], via, *common))
    if not rows:
        rows.append((MARKER_IDX, 0, "", VIA_NONE, 0, 0, "", "", 0, ""))
    return rows


def resolve_target(target: str, kind: int, paths: set[str]) -> tuple[str, bool]:
    """(path shown, exists) of a rule's target in a revision's tree: a missing `.c` object is looked for
    as `.S`, then `.rs`; a directory rule shows `dir/` and exists when any path is under it."""
    exists = target in paths
    if not exists:
        for cand in target_candidates(target)[1:]:
            if cand in paths:
                target, exists = cand, True
                break
    if kind == KIND_DIRECTORY:
        target = target[: -len("/Makefile")] + "/" if target.endswith("/Makefile") else target
        exists = exists or any(p.startswith(target) for p in paths)
    return target, exists


def usage_rule(makefile: str, line: int, rule: str, target_obj: str, target: str, kind: int, via: int,
               compile_mode: int, paths: set[str]) -> dict[str, Any]:
    """One entry of `kbuild_usage()` (webapp/API.md section 4.4)."""
    path, exists = resolve_target(target, kind, paths)
    return {
        "makefile": makefile,
        "line": line,
        "rule": rule,
        "target_obj": target_obj,
        "path": path,
        "exists": exists,
        "kind": KIND_NAMES.get(kind, "object"),
        "via": "rule" if via == VIA_RULE else "condition",
        "compile_mode": COMPILE_MODES.get(compile_mode, "conditional"),
    }


def bindings_usage(makefile: str, bindings: Iterable[Any], name: str, paths: set[str]) -> list[dict[str, Any]]:
    """The `kbuild_usage()` entries of one parsed file for CONFIG_<name> (Git path)."""
    out = []
    for b in bindings:
        if b.symbol_name != name and name not in (b.cond_symbols or ()):
            continue
        via = VIA_RULE if b.symbol_name == name else VIA_CONDITION
        out.append(usage_rule(makefile, int(b.source_line or 1), b.details or "", b.target_obj or "", b.source_file_rel or "",
                              binding_kind(b), via, int(b.compile_mode or 3), paths))
    return out
