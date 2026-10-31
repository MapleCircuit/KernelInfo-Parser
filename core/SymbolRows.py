"""core/SymbolRows.py - Symbol definitions and references stored once per tag.

A file's symbol rows used to be written per blob, so every new version of a file re-wrote the rows of all its
tags, changed or not. Here a parser records definitions and references against the tag that encloses them
(`record_def()` / `record_ref()`), and `finish()` turns them into ChangeSet operations when the file's parse ends:

- `m_tag_def` / `m_tag_ref`: the items of a new tag, with lines relative to the tag's first line. A tag reused
  from the file's previous version (tier-1 recycling in `Ast.tag()` / `CSExtractor.stage_tag()`) writes nothing.
- `m_blob_symbol`: per blob, one row per symbol key the shared rows use, resolving it to the AST id (and file)
  it means in this blob. A reference's target is a content-addressed AST id, so it changes whenever the target's
  definition changes; the key does not. Keys come from the tag's own text only (`_KeyContext.ref_key()` /
  `def_key()`): a digest of the tag's code with the identifier at the reference and its role, or the definition's
  name and type. They never depend on how far the parser resolved the target (an unresolved route in a file's
  first parse, an AST id once the database has it), so an unchanged tag compares equal in every version.
- `m_blob_symbol_def` / `m_blob_symbol_ref`: per-blob rows, as before, for items that cannot be shared: outside
  their tag's lines, without a tag, using a key that still means two targets after refinement by position (a tag
  placed twice), or a reused tag whose items differ from those stored. `m_symbol_own` marks such placements so the views skip their
  shared rows.

The `m_symbol_def` / `m_symbol_ref` views (`SYMBOL_VIEW_SQL`, created by `main.ensure_db_views()`) return both
layouts in the per-blob shape; `symbol_view_rows()` computes the same rows from in-memory tables (MockDB, tests).
`G.SYMBOL_LAYOUT = "blob"` writes every item per blob (no shared rows), e.g. to compare both layouts.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from core.DBLayout import (
    m_blob_symbol,
    m_blob_symbol_def,
    m_blob_symbol_ref,
    m_file_blob,
    m_symbol_own,
    m_tag_def,
    m_tag_ref,
)
from core.globalstuff import OP_REF, REF_POS, REF_ROOT, G

KIND_REFS = 1
KIND_DEFS = 2
# Synthetic def_id of a definition stored once per tag: bit 62 set, then blob id (25 bits), tag id (31 bits) and
# the definition's index in the tag (6 bits); a tag with more definitions keeps them per blob
DEF_ID_FLAG = 1 << 62
MAX_TAG_DEFS = 64

_BLOB_REF = ((m_file_blob.table_id, 0), OP_REF, (REF_ROOT,))

# Items written by finish() in this process, by how they were stored (e.g. "refs shared", "refs outside tag")
STATS: Counter = Counter()

_IDENTIFIER = re.compile(r"[A-Za-z0-9_$]+")


def _is_ref(val: Any) -> bool:
    return type(val) is tuple and len(val) == 3 and val[1] == OP_REF and type(val[0]) is tuple and type(val[2]) is tuple and bool(val[2])


def _norm(val: Any) -> Any:
    """Scalars as they compare in the database (IntEnum members as plain ints)."""
    if isinstance(val, bool) or not isinstance(val, int):
        return val
    return int(val)


def _digest(obj: Any, size: int) -> bytes:
    return hashlib.blake2b(repr(obj).encode("utf-8", "surrogatepass"), digest_size=size).digest()


class Placement:
    """One tag placed in the file, with the definitions and references recorded against it."""

    __slots__ = ("_digest", "_lines", "char_s", "code", "defs", "line_e", "line_s", "refs", "tag_ref")

    def __init__(self, tag_ref: Any, line_s: int, line_e: int, char_s: int = 1, code: str = "") -> None:
        self.tag_ref = tag_ref
        self.line_s = line_s
        self.line_e = line_e
        # The tag's text starts at column `char_s` (1-based) of `line_s`
        self.char_s = char_s
        self.code = code or ""
        self._lines: list[str] | None = None
        self._digest: bytes | None = None
        # (target, target_fnid, role, line, char_s)
        self.refs: list[tuple[Any, Any, int, int, int]] = []
        # (ast_ref, name, type_id, linkage, line_s, line_e)
        self.defs: list[tuple[Any, str, int, int, int, int]] = []

    @property
    def recycled(self) -> bool:
        """A tag reused from the file's previous version is a plain tag id; a new tag is a route to its m_tag op."""
        return isinstance(self.tag_ref, int)

    def inside(self, line_s: int, line_e: int | None = None) -> bool:
        return self.line_s <= line_s and (line_e if line_e is not None else line_s) <= self.line_e

    def digest(self) -> bytes:
        """Digest of the tag's text: equal for every placement of the tag, in any version."""
        if self._digest is None:
            self._digest = _digest(self.code, 8)
        return self._digest

    def token_at(self, line: int, char_s: int) -> str:
        """The identifier of the tag's text at `line`/`char_s` (1-based column), or the text there."""
        if self._lines is None:
            self._lines = self.code.split("\n")
        rel = line - self.line_s
        if not 0 <= rel < len(self._lines):
            return ""
        text = self._lines[rel]
        col = char_s - (self.char_s if rel == 0 else 1)
        if not 0 <= col < len(text):
            return ""
        match = _IDENTIFIER.match(text, col)
        return match.group(0) if match else text[col:col + 16]


class SymbolRowCollector:
    """Per-ChangeSet store of recorded symbol items, emitted by `finish()`."""

    def __init__(self) -> None:
        self.placements: dict[Any, Placement] = {}
        # Items without a registered tag: always stored per blob, as (tag_ref, item)
        self.loose_refs: list[tuple[Any, tuple[Any, Any, int, int, int]]] = []
        self.loose_defs: list[tuple[Any, tuple[Any, str, int, int, int, int]]] = []
        # tag_id -> (Counter of stored ref items, Counter of stored def items) for tags of the previous version
        self.prior_items: dict[int, tuple[Counter, Counter]] = {}
        self._fp_memo: dict[int, bytes] = {}


def collector(CS: Any) -> SymbolRowCollector:
    col = getattr(CS, "symbol_rows", None)
    if col is None:
        col = SymbolRowCollector()
        CS.symbol_rows = col
    return col


# ----------------------------------------------------------------------------------------------------------------
# Recording (called by the parsers while extracting)
# ----------------------------------------------------------------------------------------------------------------
def register_tag(CS: Any, tag_ref: Any, line_s: int, line_e: int, char_s: int = 1, code: str = "") -> None:
    """Note a tag placed in this file at lines `line_s`..`line_e` (both tier-1 recycled and new tags).

    `code` is the tag's text, starting at column `char_s` of `line_s`: the symbol keys of its items come from it.
    """
    if tag_ref is None or tag_ref == 0:
        return
    col = collector(CS)
    if tag_ref not in col.placements:
        col.placements[tag_ref] = Placement(tag_ref, int(line_s), int(line_e), int(char_s or 1), code)


def record_ref(CS: Any, tag_ref: Any, target: Any, target_fnid: Any, role: int, line: int, char_s: int) -> None:
    """Record a reference to `target` (an m_ast id or route) at `line`/`char_s`, enclosed by `tag_ref`."""
    col = collector(CS)
    item = (target, target_fnid, int(role), line, char_s)
    placement = col.placements.get(tag_ref) if tag_ref else None
    if placement is None:
        col.loose_refs.append((tag_ref or 0, item))
    else:
        placement.refs.append(item)


def flush_pending_refs(CS: Any, tag_ref: Any) -> None:
    """Record `CS.pending_symbol_refs` against `tag_ref` (repeats of one reference in the batch counted once)."""
    pending = getattr(CS, "pending_symbol_refs", None)
    if not pending:
        return
    seen: set[tuple[Any, int, int, int]] = set()
    for ref_ast_id, role, line, col in pending:
        key = (ref_ast_id, role, line, col)
        if key in seen:
            continue
        seen.add(key)
        record_ref(CS, tag_ref, ref_ast_id, 0, role, line, col)
    pending.clear()


def record_def(CS: Any, tag_ref: Any, ast_ref: Any, name: str, type_id: int, linkage: int, line_s: int, line_e: int) -> None:
    """Record a symbol definition (its AST, name, type and linkage) spanning `line_s`..`line_e` in `tag_ref`."""
    col = collector(CS)
    item = (ast_ref, name, int(type_id), int(linkage), line_s, line_e)
    placement = col.placements.get(tag_ref) if tag_ref else None
    if placement is None:
        col.loose_defs.append((tag_ref or 0, item))
    else:
        placement.defs.append(item)


def load_prior_items(CS: Any, tag_ids: Iterable[int]) -> None:
    """Load the stored items of the previous version's tags (compared with a recycled tag's current items)."""
    ids = sorted({int(t) for t in tag_ids if isinstance(t, int)})
    if not ids:
        return
    col = collector(CS)
    batch_cache = getattr(CS, "_batch_prior_items", None)
    missing_ids = []
    if batch_cache is not None:
        for tag_id in ids:
            if tag_id in batch_cache:
                col.prior_items[tag_id] = batch_cache[tag_id]
            else:
                missing_ids.append(tag_id)
    else:
        missing_ids = ids

    if not missing_ids or getattr(G, "TE", None) is None or not hasattr(G.TE, "get_in"):
        return
    refs: dict[int, Counter] = {t: Counter() for t in missing_ids}
    defs: dict[int, Counter] = {t: Counter() for t in missing_ids}
    for tag_id, _idx, key, role, rel_line, char_s in G.TE.get_in(m_tag_ref.table_id, 0, missing_ids):
        refs.setdefault(tag_id, Counter())[(key, role, rel_line, char_s)] += 1
    for tag_id, _idx, key, name, type_id, linkage, rel_s, rel_e in G.TE.get_in(m_tag_def.table_id, 0, missing_ids):
        defs.setdefault(tag_id, Counter())[(key, _decode(name), type_id, linkage, rel_s, rel_e)] += 1
    for tag_id in missing_ids:
        col.prior_items[tag_id] = (refs[tag_id], defs[tag_id])


def _decode(val: Any) -> Any:
    return val.decode("utf-8", "replace") if isinstance(val, (bytes, bytearray)) else val


# ----------------------------------------------------------------------------------------------------------------
# Keys and fingerprints
# ----------------------------------------------------------------------------------------------------------------
class _KeyContext:
    """Keys of recorded items, and target fingerprints (which targets are the same) over one ChangeSet's operations."""

    def __init__(self, ops: list[Any], col: SymbolRowCollector) -> None:
        self.ops = ops
        self.col = col

    def _op(self, idx: Any) -> Any:
        if type(idx) is int and 0 <= idx < len(self.ops):
            return self.ops[idx]
        return None

    @staticmethod
    def ref_key(p: Placement, item: tuple[Any, Any, int, int, int]) -> int:
        """sym_key of a reference: the tag's text, the identifier at the reference and its role."""
        _target, _fnid, role, line, char_s = item
        return _key(("r", p.digest(), p.token_at(line, char_s), role))

    @staticmethod
    def def_key(p: Placement, item: tuple[Any, str, int, int, int, int]) -> int:
        """sym_key of a definition: the tag's text, the definition's name and type."""
        _ast, name, type_id, _linkage, _line_s, _line_e = item
        return _key(("d", p.digest(), name, type_id))

    def _positional_children(self, val: Any) -> Iterable[int]:
        if _is_ref(val):
            route = val[2]
            if route[0] == REF_POS and len(route) == 2 and type(route[1]) is int:
                yield route[1]
        elif type(val) is tuple:
            for v in val:
                yield from self._positional_children(v)

    def _op_fp(self, root: int) -> bytes:
        """Merkle fingerprint of an operation: equal fingerprints resolve to the same content-addressed rows."""
        memo = self.col._fp_memo
        if root in memo:
            return memo[root]
        stack = [root]
        expanding: set[int] = set()
        while stack:
            idx = stack[-1]
            if idx in memo:
                stack.pop()
                continue
            op = self._op(idx)
            if op is None:
                memo[idx] = _digest(("missing", idx), 16)
                stack.pop()
                continue
            pending = [
                c for c in dict.fromkeys(self._positional_children(op[2]))
                if c not in memo and c not in expanding and c != idx and self._op(c) is not None
            ]
            if pending and idx not in expanding:
                expanding.add(idx)
                stack.extend(pending)
                continue
            memo[idx] = _digest((op[0], op[1], self._fp_val(op[2])), 16)
            expanding.discard(idx)
            stack.pop()
        return memo[root]

    def _fp_val(self, val: Any) -> Any:
        if _is_ref(val):
            q, _, route = val
            if route[0] == REF_POS and len(route) == 2 and type(route[1]) is int:
                fp = self.col._fp_memo.get(route[1])
                return ("P", q, fp if fp is not None else ("cycle", route[1]))
            return ("R", q, route)
        if type(val) is tuple:
            return tuple(self._fp_val(v) for v in val)
        return _norm(val)

    def fingerprint(self, val: Any) -> Any:
        """Identity of a target within this parse (only compared within one ChangeSet)."""
        if _is_ref(val):
            route = val[2]
            if route[0] == REF_POS and len(route) == 2 and type(route[1]) is int and self._op(route[1]) is not None:
                return ("P", val[0], self._op_fp(route[1]))
            return ("R", val[0], route)
        return ("v", _norm(val))


# ----------------------------------------------------------------------------------------------------------------
# Emission
# ----------------------------------------------------------------------------------------------------------------
def _attribute_to_enclosing_tags(col: SymbolRowCollector) -> None:
    """Move items recorded against a tag that does not contain them to the innermost tag that does.

    Pending references are flushed to the next tag created, which is often a later tag (e.g. a one-line
    statement after the function body's references); an item no tag contains stays where it was.
    """
    placements = list(col.placements.values())
    if not placements:
        return
    innermost: dict[int, Placement] = {}
    for p in sorted(placements, key=lambda p: p.line_e - p.line_s, reverse=True):
        for line in range(p.line_s, p.line_e + 1):
            innermost[line] = p
    moved_refs: list[tuple[Placement, tuple[Any, Any, int, int, int]]] = []
    moved_defs: list[tuple[Placement, tuple[Any, str, int, int, int, int]]] = []
    for p in placements:
        keep = []
        for item in p.refs:
            owner = p if p.inside(item[3]) else innermost.get(item[3])
            if owner is None or owner is p:
                keep.append(item)
            else:
                moved_refs.append((owner, item))
        p.refs = keep
        keep_defs = []
        for item in p.defs:
            owner = p if p.inside(item[4], item[5]) else innermost.get(item[4])
            if owner is None or owner is p or not owner.inside(item[4], item[5]):
                keep_defs.append(item)
            else:
                moved_defs.append((owner, item))
        p.defs = keep_defs
    for owner, item in moved_refs:
        owner.refs.append(item)
    for owner, item in moved_defs:
        owner.defs.append(item)


def finish(CS: Any) -> None:
    """Turn the recorded symbol items of this ChangeSet into operations (see the module docstring)."""
    col = getattr(CS, "symbol_rows", None)
    if col is None:
        return
    CS.symbol_rows = None
    if not col.placements and not col.loose_refs and not col.loose_defs:
        return
    shared = getattr(G, "SYMBOL_LAYOUT", "tag") != "blob"
    ctx = _KeyContext(CS.cs, col)
    _attribute_to_enclosing_tags(col)

    # 1. Items inside their tag get keys; the others are stored per blob
    plans = []
    blob_refs: list[tuple[Any, tuple[Any, Any, int, int, int]]] = list(col.loose_refs)
    blob_defs: list[tuple[Any, tuple[Any, str, int, int, int, int]]] = list(col.loose_defs)
    STATS["refs without tag"] += len(col.loose_refs)
    STATS["defs without tag"] += len(col.loose_defs)
    first_target: dict[int, tuple[Any, Any]] = {}
    for p in col.placements.values():
        refs, defs = [], []
        for item in p.refs:
            if shared and p.inside(item[3]):
                refs.append((ctx.ref_key(p, item), item))
            else:
                blob_refs.append((p.tag_ref, item))
                STATS["refs outside tag"] += 1
        for item in p.defs:
            if shared and p.inside(item[4], item[5]):
                defs.append((ctx.def_key(p, item), item))
            else:
                blob_defs.append((p.tag_ref, item))
                STATS["defs outside tag"] += 1
        if len(defs) > MAX_TAG_DEFS:
            blob_defs.extend((p.tag_ref, item) for _key, item in defs)
            defs = []
        plans.append((p, refs, defs))

    def _targets() -> dict[int, set[Any]]:
        raw: dict[int, set[tuple[Any, Any]]] = {}
        for _p, refs, defs in plans:
            for key, item in refs:
                raw.setdefault(key, set()).add((item[0], item[1]))
            for key, item in defs:
                raw.setdefault(key, set()).add((item[0], 0))
        found: dict[int, set[Any]] = {}
        for key, targets in raw.items():
            if len(targets) <= 1:
                found[key] = {0}
            else:
                found[key] = {(ctx.fingerprint(t0), ctx.fingerprint(t1)) for t0, t1 in targets}
        return found

    # A key meaning two different targets in this blob (e.g. a shadowed local) cannot be resolved by one
    # m_blob_symbol row: its items get a key refined by their position in the tag, which the tag's text fixes too
    ambiguous = {key for key, fps in _targets().items() if len(fps) > 1}
    conflicts: set[int] = set()
    if ambiguous:
        for idx, (p, refs, defs) in enumerate(plans):
            refs = [(_refined(key, item[3] - p.line_s, item[4]) if key in ambiguous else key, item) for key, item in refs]
            defs = [(_refined(key, item[4] - p.line_s, item[5] - p.line_s) if key in ambiguous else key, item) for key, item in defs]
            plans[idx] = (p, refs, defs)
        STATS["keys refined"] += len(ambiguous)
        # Still two targets: the same tag placed twice in this blob, resolved differently. Its placements keep
        # their items per blob.
        conflicts = {key for key, fps in _targets().items() if len(fps) > 1}
        STATS["keys conflicting"] += len(conflicts)
    for _p, refs, defs in plans:
        for key, item in refs:
            first_target.setdefault(key, (item[0], item[1]))
        for key, item in defs:
            first_target.setdefault(key, (item[0], 0))

    mapped: set[int] = set()
    for p, refs, defs in plans:
        stored = col.prior_items.get(p.tag_ref) if p.recycled else None
        if not shared:
            # Every item is stored per blob; a tag stored under the shared layout must not show its rows here
            if stored is not None:
                for kind, stored_items in ((KIND_REFS, stored[0]), (KIND_DEFS, stored[1])):
                    if stored_items:
                        _store(CS, m_symbol_own.set(_BLOB_REF, p.tag_ref, kind))
            continue
        for kind, items in ((KIND_REFS, refs), (KIND_DEFS, defs)):
            current = Counter(_shared_item(kind, key, item, p.line_s) for key, item in items)
            if not p.recycled:
                # A new tag stores its items for later versions, even when this blob keeps them itself
                for idx, (key, item) in enumerate(items):
                    _store(CS, _tag_row(kind, p.tag_ref, idx, key, item, p.line_s))
                has_shared_rows = bool(items)
                own = False
            else:
                stored_items = stored[0 if kind == KIND_REFS else 1] if stored is not None else None
                has_shared_rows = bool(stored_items)
                own = stored_items is None or current != stored_items
                if not items and not stored_items:
                    own = False
            conflict = bool(conflicts) and any(key in conflicts for key, _item in items)
            name = "refs" if kind == KIND_REFS else "defs"
            if own or conflict:
                STATS[f"{name} {'conflicting key' if conflict else 'changed reused tag'}"] += len(items)
                if has_shared_rows:
                    _store(CS, m_symbol_own.set(_BLOB_REF, p.tag_ref, kind))
                for _key, item in items:
                    (blob_refs if kind == KIND_REFS else blob_defs).append((p.tag_ref, item))
                continue
            STATS[f"{name} {'reused' if p.recycled else 'new tag'}"] += len(items)
            for key, _item in items:
                if key not in mapped:
                    mapped.add(key)
                    target, fnid = first_target[key]
                    _store(CS, m_blob_symbol.set(_BLOB_REF, key, target, fnid))

    for tag_ref, (target, fnid, role, line, char_s) in blob_refs:
        _store(CS, m_blob_symbol_ref.set(None, _BLOB_REF, tag_ref, target, fnid, role, line, char_s))
    for tag_ref, (ast_ref, name, type_id, linkage, line_s, line_e) in blob_defs:
        _store(CS, m_blob_symbol_def.set(None, _BLOB_REF, tag_ref, ast_ref, name, type_id, linkage, line_s, line_e))


def _store(CS: Any, op: Any) -> None:
    """Store one operation positionally (each in its own REF_POS scope, like the parsers' own symbol stores)."""
    with CS(REF_POS):
        CS.store(op)


def _key(obj: Any) -> int:
    """64-bit signed sym_key of a description."""
    return int.from_bytes(_digest(obj, 8), "big", signed=True)


def _refined(key: int, rel_a: int, rel_b: int) -> int:
    """Key of an item whose key is ambiguous in its blob, distinguished by its position relative to its tag."""
    return _key(("at", key, rel_a, rel_b))


def _shared_item(kind: int, key: int, item: tuple[Any, ...], tag_line_s: int) -> tuple[Any, ...]:
    """The stored form of an item (as loaded by `load_prior_items()`), relative to its tag."""
    if kind == KIND_REFS:
        _target, _fnid, role, line, char_s = item
        return (key, role, line - tag_line_s, char_s)
    _ast, name, type_id, linkage, line_s, line_e = item
    return (key, name, type_id, linkage, line_s - tag_line_s, line_e - tag_line_s)


def _tag_row(kind: int, tag_ref: Any, idx: int, key: int, item: tuple[Any, ...], tag_line_s: int) -> Any:
    if kind == KIND_REFS:
        _target, _fnid, role, line, char_s = item
        return m_tag_ref.set(tag_ref, idx, key, role, line - tag_line_s, char_s)
    _ast, name, type_id, linkage, line_s, line_e = item
    return m_tag_def.set(tag_ref, idx, key, name, type_id, linkage, line_s - tag_line_s, line_e - tag_line_s)


# ----------------------------------------------------------------------------------------------------------------
# Reading: database views and their in-memory equivalent
# ----------------------------------------------------------------------------------------------------------------
_OWN = "NOT EXISTS (SELECT 1 FROM m_symbol_own o WHERE o.blob_id = bt.blob_id AND o.tag_id = bt.tag_id AND o.kind = {kind})"
# Per view: (columns of its per-blob rows, columns of its shared rows, the tag table of the shared rows, its kind)
_VIEW_PARTS: dict[str, tuple[str, str, str, int]] = {
    "m_symbol_def": (
        "d.def_id, d.blob_id, d.tag_id, d.ast_id, d.name, d.type_id, d.linkage, d.line_s, d.line_e FROM m_blob_symbol_def d",
        f"CAST({DEF_ID_FLAG} | (bt.blob_id << 37) | (ti.tag_id << 6) | ti.def_idx AS SIGNED) AS def_id, "
        "bt.blob_id, ti.tag_id, bs.target_ast_id AS ast_id, ti.name, ti.type_id, ti.linkage, "
        "bt.line_s + ti.rel_line_s AS line_s, bt.line_s + ti.rel_line_e AS line_e",
        "m_tag_def",
        KIND_DEFS,
    ),
    "m_symbol_ref": (
        "r.ref_id, r.blob_id, r.tag_id, r.ast_id, r.target_fnid, r.role, r.line, r.char_s FROM m_blob_symbol_ref r",
        "NULL AS ref_id, bt.blob_id, ti.tag_id, bs.target_ast_id AS ast_id, bs.target_fnid, ti.role, "
        "bt.line_s + ti.rel_line AS line, ti.char_s",
        "m_tag_ref",
        KIND_REFS,
    ),
}

SYMBOL_VIEW_SQL: dict[str, str] = {
    view: (
        f"CREATE OR REPLACE VIEW {view} AS SELECT {blob_cols} UNION ALL SELECT {shared_cols} "
        f"FROM m_bridge_tag bt JOIN {tag_table} ti ON ti.tag_id = bt.tag_id "
        "JOIN m_blob_symbol bs ON bs.blob_id = bt.blob_id AND bs.sym_key = ti.sym_key "
        f"WHERE {_OWN.format(kind=kind)};"
    )
    for view, (blob_cols, shared_cols, tag_table, kind) in _VIEW_PARTS.items()
}


def rows_of_targets_sql(view: str, n_targets: int) -> tuple[str, int]:
    """A SELECT of the `view` rows whose `ast_id` is one of `n_targets` AST ids, and how often to pass them.

    Filtering the view by `ast_id` makes the database join each blob that uses a target with every tag of every
    blob holding the key (seconds to minutes for a common function). This query starts from the target's keys,
    then their tag rows, their placements and the placements' blob resolution. Use it as a derived table:
    `cursor.execute(f"SELECT ... FROM ({sql}) r", tuple(ids) * repeat)`.
    """
    blob_cols, shared_cols, tag_table, kind = _VIEW_PARTS[view]
    ids = ", ".join(["%s"] * n_targets)
    sql = (
        f"SELECT {blob_cols} WHERE {'d' if view == 'm_symbol_def' else 'r'}.ast_id IN ({ids}) UNION ALL "
        f"SELECT STRAIGHT_JOIN {shared_cols} "
        f"FROM (SELECT DISTINCT sym_key FROM m_blob_symbol WHERE target_ast_id IN ({ids})) k "
        f"JOIN {tag_table} ti ON ti.sym_key = k.sym_key "
        "JOIN m_bridge_tag bt ON bt.tag_id = ti.tag_id "
        "JOIN m_blob_symbol bs ON bs.blob_id = bt.blob_id AND bs.sym_key = ti.sym_key "
        f"WHERE bs.target_ast_id IN ({ids}) AND {_OWN.format(kind=kind)}"
    )
    return sql, 3


def synthetic_def_id(blob_id: int, tag_id: int, def_idx: int) -> int:
    """def_id the m_symbol_def view gives a definition stored once per tag."""
    return DEF_ID_FLAG | (blob_id << 37) | (tag_id << 6) | def_idx


def symbol_view_rows(tables: Mapping[str, Iterable[tuple[Any, ...]]], view: str) -> list[tuple[Any, ...]]:
    """Rows of the `m_symbol_def` / `m_symbol_ref` view computed from table rows (e.g. `MockDB._global_store`).

    `tables` maps table names to their rows (dict values or lists of tuples). Per-blob rows come first, then the
    shared rows ordered by blob, tag placement and index.
    """
    def rows(name: str) -> list[tuple[Any, ...]]:
        data = tables.get(name) or ()
        return list(data.values()) if isinstance(data, Mapping) else list(data)

    own = {(r[0], r[1], r[2]) for r in rows("m_symbol_own")}
    resolve = {(r[0], r[1]): (r[2], r[3]) for r in rows("m_blob_symbol")}
    placements = sorted(rows("m_bridge_tag"), key=lambda r: (r[0], r[2], r[1]))
    out: list[tuple[Any, ...]] = []
    if view == "m_symbol_def":
        out.extend(rows("m_blob_symbol_def"))
        by_tag: dict[int, list[tuple[Any, ...]]] = {}
        for r in sorted(rows("m_tag_def"), key=lambda r: (r[0], r[1])):
            by_tag.setdefault(r[0], []).append(r)
        for blob_id, tag_id, line_s, *_rest in placements:
            if (blob_id, tag_id, KIND_DEFS) in own:
                continue
            for _tag, idx, key, name, type_id, linkage, rel_s, rel_e in by_tag.get(tag_id, ()):
                hit = resolve.get((blob_id, key))
                if hit is not None:
                    out.append((synthetic_def_id(blob_id, tag_id, idx), blob_id, tag_id, hit[0], name, type_id, linkage, line_s + rel_s, line_s + rel_e))
        return out
    if view == "m_symbol_ref":
        out.extend(rows("m_blob_symbol_ref"))
        by_tag = {}
        for r in sorted(rows("m_tag_ref"), key=lambda r: (r[0], r[1])):
            by_tag.setdefault(r[0], []).append(r)
        for blob_id, tag_id, line_s, *_rest in placements:
            if (blob_id, tag_id, KIND_REFS) in own:
                continue
            for _tag, _idx, key, role, rel_line, char_s in by_tag.get(tag_id, ()):
                hit = resolve.get((blob_id, key))
                if hit is not None:
                    out.append((None, blob_id, tag_id, hit[0], hit[1], role, line_s + rel_line, char_s))
        return out
    raise ValueError(f"unknown symbol view '{view}'")
