"""cache_engine/ram_cache.py - Pure-Python in-RAM implementation of the cache engine (cache_engine/CACHE_API.md).

Stores records in Python dicts with inline-1-or-set hash indexes, lazy chunked sorted indexes for range/prefix/order_by
queries, copy-on-write MVCC snapshots for lock-free concurrent readers, and re-entrant atomic ``batch()`` transactions.
"""
from __future__ import annotations

import bisect
import contextlib
import heapq
import operator
from pathlib import Path
import threading
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .base import (BaseCacheEngine, CacheSpace, CacheStore, Condition, Eq, FieldType, In, IsNull, Key, Prefix, Range,
                   Record, StoreSpec, Where, as_conditions, check_namespace)
from .errors import CacheError, CacheSchemaError

IN_CHUNK = 500
_SORTED_LOAD = 1024
_SORTED_SPLIT = 2048
_BATCH_DELTA_THRESHOLD = 4096
_BULK_INVALIDATE_RATIO = 0.25


def _prefix_bounds(value: Any) -> tuple[Any, Any]:
    """(lower, exclusive upper or None) of the values starting with ``value``."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        lo = bytes(value)
        n = int.from_bytes(lo, "big") + 1
        hi = n.to_bytes(len(lo), "big") if lo and n < 1 << (8 * len(lo)) else None
        return lo, hi
    if isinstance(value, str):
        return value, value + "\U0010ffff"
    raise CacheSchemaError(f"Prefix needs a text or bytes value, got {type(value).__name__}")


def _normalize_prefix(value: Any) -> str | bytes:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, str):
        return value
    raise CacheSchemaError(f"Prefix needs a text or bytes value, got {type(value).__name__}")


class _StoreSchema:
    """Precomputed field positions, key extractors, unique groups, and index definitions for a ``StoreSpec``."""

    __slots__ = (
        "spec", "name", "n_fields", "field_names", "field_pos", "field_types",
        "field_nullable", "non_null_pos", "key_pos", "key_single", "key_getter",
        "unique_groups", "unique_extractors", "index_groups", "index_extractors",
        "simple_keyed",
    )

    def __init__(self, spec: StoreSpec) -> None:
        self.spec = spec
        self.name = spec.name
        self.n_fields = len(spec.fields)
        self.field_names = spec.field_names
        self.field_pos = {f.name: i for i, f in enumerate(spec.fields)}
        self.field_types = tuple(f.type for f in spec.fields)
        self.field_nullable = tuple(f.nullable for f in spec.fields)
        self.non_null_pos = tuple(i for i, n in enumerate(self.field_nullable) if not n)
        self.key_pos = tuple(self.field_pos[k] for k in spec.key)
        self.key_single: int | None = self.key_pos[0] if len(self.key_pos) == 1 else None
        self.key_getter: Callable[[Record], Key] | None = (
            operator.itemgetter(self.key_pos[0]) if len(self.key_pos) == 1
            else (operator.itemgetter(*self.key_pos) if self.key_pos else None)
        )

        self.unique_groups: list[tuple[tuple[int, ...], bool]] = []
        self.unique_extractors: list[tuple[Callable[[Record], Any], bool, bool]] = []
        for grp in spec.unique:
            pos = tuple(self.field_pos[g] for g in grp)
            has_null = any(self.field_nullable[p] for p in pos)
            is_single = len(pos) == 1
            getter = operator.itemgetter(pos[0]) if is_single else operator.itemgetter(*pos)
            self.unique_groups.append((pos, has_null))
            self.unique_extractors.append((getter, has_null, is_single))

        idx_set: list[tuple[int, ...]] = []
        seen: set[tuple[int, ...]] = set()
        for grp in spec.indexes:
            pos = tuple(self.field_pos[g] for g in grp)
            if pos not in seen:
                seen.add(pos)
                idx_set.append(pos)
            if len(pos) > 1 and (pos[0],) not in seen:
                seen.add((pos[0],))
                idx_set.append((pos[0],))
        if len(self.key_pos) > 1 and (self.key_pos[0],) not in seen:
            seen.add((self.key_pos[0],))
            idx_set.append((self.key_pos[0],))
        for u_pos, _ in self.unique_groups:
            if len(u_pos) > 1 and (u_pos[0],) not in seen:
                seen.add((u_pos[0],))
                idx_set.append((u_pos[0],))
        self.index_groups = tuple(idx_set)
        self.index_extractors: tuple[tuple[tuple[int, ...], Callable[[Record], Any]], ...] = tuple(
            (grp, operator.itemgetter(grp[0]) if len(grp) == 1 else operator.itemgetter(*grp))
            for grp in idx_set
        )
        self.simple_keyed = bool(self.key_pos) and not self.unique_groups

    def extract_key(self, rec: Record) -> Key:
        return self.key_getter(rec)  # type: ignore[misc]

    def validate_record(self, rec: Any) -> Record:
        if type(rec) is tuple:
            out = rec
        elif isinstance(rec, list):
            out = tuple(rec)
        else:
            raise CacheSchemaError(f"store {self.name!r}: record must be a tuple, got {type(rec).__name__}")
        if len(out) != self.n_fields:
            raise CacheSchemaError(
                f"store {self.name!r}: expected {self.n_fields} fields, got {len(out)}")
        for i in self.non_null_pos:
            if out[i] is None:
                raise CacheSchemaError(
                    f"store {self.name!r}: field {self.field_names[i]!r} is not nullable")
        return out

    def check_field(self, name: str) -> int:
        pos = self.field_pos.get(name)
        if pos is None:
            raise CacheSchemaError(f"store {self.name!r} has no field {name!r}")
        return pos

    def validate_key(self, key: Any) -> Key:
        if not self.key_pos:
            raise CacheSchemaError(f"store {self.name!r} has no key")
        if self.key_single is None:
            if not isinstance(key, tuple) or len(key) != len(self.key_pos):
                raise CacheSchemaError(
                    f"store {self.name!r}: key must be a tuple of {len(self.key_pos)} values")
        try:
            hash(key)
        except TypeError as exc:
            raise CacheSchemaError(f"store {self.name!r}: unhashable key: {exc}") from exc
        return key


class _SortedIndex:
    """Chunked sorted index with ``bisect`` for O(log N) range/prefix/order_by lookups.

    Parallel arrays ``_vals``, ``_hashes``, ``_pks`` store non-None values sorted by ``(val, hash(pk))``;
    ``_nulls`` holds keys whose indexed field is ``None``.
    """

    __slots__ = ("_vals", "_hashes", "_pks", "_maxes", "_max_hashes", "_nulls")

    def __init__(self) -> None:
        self._vals: list[list[Any]] = []
        self._hashes: list[list[int]] = []
        self._pks: list[list[Key]] = []
        self._maxes: list[Any] = []
        self._max_hashes: list[int] = []
        self._nulls: set[Key] = set()

    @classmethod
    def build(cls, rows: dict[Key, Record], col: int) -> "_SortedIndex":
        idx = cls()
        items: list[tuple[Any, int, Key]] = []
        nulls = idx._nulls
        for pk, rec in rows.items():
            v = rec[col]
            if v is None:
                nulls.add(pk)
            else:
                items.append((v, hash(pk), pk))
        if items:
            items.sort(key=operator.itemgetter(0, 1))
            for i in range(0, len(items), _SORTED_LOAD):
                chunk = items[i:i + _SORTED_LOAD]
                vs = [t[0] for t in chunk]
                hs = [t[1] for t in chunk]
                ps = [t[2] for t in chunk]
                idx._vals.append(vs)
                idx._hashes.append(hs)
                idx._pks.append(ps)
                idx._maxes.append(vs[-1])
                idx._max_hashes.append(hs[-1])
        return idx

    def copy(self) -> "_SortedIndex":
        out = _SortedIndex()
        out._vals = [list(c) for c in self._vals]
        out._hashes = [list(c) for c in self._hashes]
        out._pks = [list(c) for c in self._pks]
        out._maxes = list(self._maxes)
        out._max_hashes = list(self._max_hashes)
        out._nulls = set(self._nulls)
        return out

    def _find_chunk(self, val: Any, h: int) -> int:
        c = bisect.bisect_left(self._maxes, val)
        n = len(self._maxes)
        if c >= n:
            return n - 1
        if self._maxes[c] == val and self._max_hashes[c] < h:
            hi = bisect.bisect_right(self._maxes, val, lo=c)
            c2 = bisect.bisect_left(self._max_hashes, h, lo=c, hi=hi)
            return min(c2, n - 1)
        return c

    def add(self, val: Any, pk: Key) -> None:
        if val is None:
            self._nulls.add(pk)
            return
        h = hash(pk)
        if not self._vals:
            self._vals.append([val])
            self._hashes.append([h])
            self._pks.append([pk])
            self._maxes.append(val)
            self._max_hashes.append(h)
            return
        c = self._find_chunk(val, h)
        vs = self._vals[c]
        hs = self._hashes[c]
        ps = self._pks[c]
        lo = bisect.bisect_left(vs, val)
        hi = bisect.bisect_right(vs, val, lo)
        pos = bisect.bisect_left(hs, h, lo, hi)
        vs.insert(pos, val)
        hs.insert(pos, h)
        ps.insert(pos, pk)
        if pos == len(vs) - 1:
            self._maxes[c] = val
            self._max_hashes[c] = h
        if len(vs) >= _SORTED_SPLIT:
            mid = _SORTED_LOAD
            self._vals.insert(c + 1, vs[mid:])
            self._hashes.insert(c + 1, hs[mid:])
            self._pks.insert(c + 1, ps[mid:])
            del vs[mid:]
            del hs[mid:]
            del ps[mid:]
            self._maxes[c] = vs[-1]
            self._max_hashes[c] = hs[-1]
            self._maxes.insert(c + 1, self._vals[c + 1][-1])
            self._max_hashes.insert(c + 1, self._hashes[c + 1][-1])

    def remove(self, val: Any, pk: Key) -> None:
        if val is None:
            self._nulls.discard(pk)
            return
        if not self._vals:
            return
        h = hash(pk)
        c = self._find_chunk(val, h)
        n_chunks = len(self._vals)
        while c < n_chunks:
            vs = self._vals[c]
            hs = self._hashes[c]
            ps = self._pks[c]
            lo = bisect.bisect_left(vs, val)
            hi = bisect.bisect_right(vs, val, lo)
            pos = bisect.bisect_left(hs, h, lo, hi)
            while pos < hi and hs[pos] == h:
                if ps[pos] == pk:
                    vs.pop(pos)
                    hs.pop(pos)
                    ps.pop(pos)
                    if not vs:
                        del self._vals[c]
                        del self._hashes[c]
                        del self._pks[c]
                        del self._maxes[c]
                        del self._max_hashes[c]
                    elif pos == len(vs):
                        self._maxes[c] = vs[-1]
                        self._max_hashes[c] = hs[-1]
                    return
                pos += 1
            if self._maxes[c] == val and self._max_hashes[c] == h:
                c += 1
            else:
                break

    def range_pks(self, ge: Any = None, gt: Any = None, le: Any = None, lt: Any = None) -> Iterator[Key]:
        if not self._vals:
            return
        # Determine strictest lower bound
        has_lo = False
        lo_val = None
        lo_inc = True
        if ge is not None and gt is not None:
            has_lo = True
            if gt >= ge:
                lo_val, lo_inc = gt, False
            else:
                lo_val, lo_inc = ge, True
        elif ge is not None:
            has_lo, lo_val, lo_inc = True, ge, True
        elif gt is not None:
            has_lo, lo_val, lo_inc = True, gt, False

        # Determine strictest upper bound
        has_hi = False
        hi_val = None
        hi_inc = True
        if le is not None and lt is not None:
            has_hi = True
            if lt <= le:
                hi_val, hi_inc = lt, False
            else:
                hi_val, hi_inc = le, True
        elif le is not None:
            has_hi, hi_val, hi_inc = True, le, True
        elif lt is not None:
            has_hi, hi_val, hi_inc = True, lt, False

        if has_lo and has_hi:
            if lo_val > hi_val or (lo_val == hi_val and not (lo_inc and hi_inc)):
                return

        if has_lo:
            c = bisect.bisect_left(self._maxes, lo_val) if lo_inc else bisect.bisect_right(self._maxes, lo_val)
            if c >= len(self._vals):
                return
            vs = self._vals[c]
            idx = bisect.bisect_left(vs, lo_val) if lo_inc else bisect.bisect_right(vs, lo_val)
        else:
            c = 0
            idx = 0

        n_chunks = len(self._vals)
        while c < n_chunks:
            vs = self._vals[c]
            ps = self._pks[c]
            if not has_hi:
                yield from ps[idx:]
            else:
                end = bisect.bisect_right(vs, hi_val, idx) if hi_inc else bisect.bisect_left(vs, hi_val, idx)
                yield from ps[idx:end]
                if end < len(vs):
                    return
            c += 1
            idx = 0

    def iter_order(self, desc: bool) -> Iterator[Key]:
        if desc:
            for ps in reversed(self._pks):
                yield from reversed(ps)
            yield from self._nulls
        else:
            yield from self._nulls
            for ps in self._pks:
                yield from ps

    def range_count(self, ge: Any = None, gt: Any = None, le: Any = None, lt: Any = None) -> int:
        if not self._vals:
            return 0
        has_lo = False
        lo_val = None
        lo_inc = True
        if ge is not None and gt is not None:
            has_lo = True
            if gt >= ge:
                lo_val, lo_inc = gt, False
            else:
                lo_val, lo_inc = ge, True
        elif ge is not None:
            has_lo, lo_val, lo_inc = True, ge, True
        elif gt is not None:
            has_lo, lo_val, lo_inc = True, gt, False

        has_hi = False
        hi_val = None
        hi_inc = True
        if le is not None and lt is not None:
            has_hi = True
            if lt <= le:
                hi_val, hi_inc = lt, False
            else:
                hi_val, hi_inc = le, True
        elif le is not None:
            has_hi, hi_val, hi_inc = True, le, True
        elif lt is not None:
            has_hi, hi_val, hi_inc = True, lt, False

        if has_lo and has_hi:
            if lo_val > hi_val or (lo_val == hi_val and not (lo_inc and hi_inc)):
                return 0

        if has_lo:
            c_start = bisect.bisect_left(self._maxes, lo_val) if lo_inc else bisect.bisect_right(self._maxes, lo_val)
            if c_start >= len(self._vals):
                return 0
            vs = self._vals[c_start]
            idx_start = bisect.bisect_left(vs, lo_val) if lo_inc else bisect.bisect_right(vs, lo_val)
        else:
            c_start = 0
            idx_start = 0

        if has_hi:
            c_end = bisect.bisect_right(self._maxes, hi_val) if hi_inc else bisect.bisect_left(self._maxes, hi_val)
            if c_end >= len(self._vals):
                c_end = len(self._vals) - 1
                idx_end = len(self._vals[c_end])
            else:
                vs_end = self._vals[c_end]
                idx_end = bisect.bisect_right(vs_end, hi_val) if hi_inc else bisect.bisect_left(vs_end, hi_val)
        else:
            c_end = len(self._vals) - 1
            idx_end = len(self._vals[c_end])

        if c_start > c_end:
            return 0
        if c_start == c_end:
            return max(0, idx_end - idx_start)
        total = (len(self._vals[c_start]) - idx_start) + idx_end
        for c in range(c_start + 1, c_end):
            total += len(self._vals[c])
        return total

    def needs_compact(self) -> bool:
        if len(self._vals) <= 1:
            return False
        total = sum(len(c) for c in self._vals)
        return total < len(self._vals) * (_SORTED_LOAD // 2) or any(len(c) < _SORTED_LOAD // 4 for c in self._vals)

    def compact(self) -> None:
        if not self.needs_compact():
            return
        all_v: list[Any] = []
        all_h: list[int] = []
        all_p: list[Key] = []
        for vs, hs, ps in zip(self._vals, self._hashes, self._pks):
            all_v.extend(vs)
            all_h.extend(hs)
            all_p.extend(ps)
        self._vals = [all_v[i:i + _SORTED_LOAD] for i in range(0, len(all_v), _SORTED_LOAD)]
        self._hashes = [all_h[i:i + _SORTED_LOAD] for i in range(0, len(all_h), _SORTED_LOAD)]
        self._pks = [all_p[i:i + _SORTED_LOAD] for i in range(0, len(all_p), _SORTED_LOAD)]
        self._maxes = [c[-1] for c in self._vals]
        self._max_hashes = [c[-1] for c in self._hashes]


def _idx_add(idx: dict[Any, Any], val: Any, pk: Key) -> None:
    cur = idx.get(val)
    if cur is None:
        # Store scalar pk directly when cardinality == 1 (unless pk itself is a set, which keys never are)
        idx[val] = pk
    elif type(cur) is set:
        cur.add(pk)
    elif cur != pk:
        idx[val] = {cur, pk}


def _idx_remove(idx: dict[Any, Any], val: Any, pk: Key) -> None:
    cur = idx.get(val)
    if cur is None:
        return
    if type(cur) is set:
        cur.discard(pk)
        if not cur:
            del idx[val]
        elif len(cur) == 1:
            idx[val] = next(iter(cur))
    elif cur == pk:
        del idx[val]


def _idx_iter(idx: dict[Any, Any], val: Any) -> Iterable[Key]:
    cur = idx.get(val)
    if cur is None:
        return ()
    if type(cur) is set:
        return cur
    return (cur,)


def _idx_len(idx: dict[Any, Any], val: Any) -> int:
    cur = idx.get(val)
    if cur is None:
        return 0
    if type(cur) is set:
        return len(cur)
    return 1


class _StoreState:
    """Snapshot of one store's rows and secondary indexes."""

    __slots__ = ("schema", "rows", "next_rid", "uniques", "indexes", "sorted_indexes", "readers", "deleted_since_compact")

    def __init__(self, schema: _StoreSchema) -> None:
        self.schema = schema
        self.rows: dict[Key, Record] = {}
        self.next_rid: int = 0
        self.uniques: list[dict[Any, Key]] = [{} for _ in schema.unique_groups]
        self.indexes: dict[tuple[int, ...], dict[Any, Any]] = {g: {} for g in schema.index_groups}
        self.sorted_indexes: dict[int, _SortedIndex] = {}
        self.readers: int = 0
        self.deleted_since_compact: int = 0

    def clone(self) -> "_StoreState":
        out = _StoreState.__new__(_StoreState)
        out.schema = self.schema
        out.rows = dict(self.rows)
        out.next_rid = self.next_rid
        out.uniques = [dict(u) for u in self.uniques]
        out.indexes = {
            g: {k: (set(v) if type(v) is set else v) for k, v in idx.items()}
            for g, idx in self.indexes.items()
        }
        out.sorted_indexes = {col: si.copy() for col, si in self.sorted_indexes.items()}
        out.readers = 0
        out.deleted_since_compact = self.deleted_since_compact
        return out

    def _index_add(self, pk: Key, rec: Record, *, update_sorted: bool = True) -> None:
        schema = self.schema
        for u_dict, (u_get, has_null, is_single) in zip(self.uniques, schema.unique_extractors):
            uv = u_get(rec)
            if has_null and (uv is None if is_single else None in uv):
                continue
            u_dict[uv] = pk
        for grp, idx_get in schema.index_extractors:
            _idx_add(self.indexes[grp], idx_get(rec), pk)
        if update_sorted and self.sorted_indexes:
            for col, si in self.sorted_indexes.items():
                si.add(rec[col], pk)

    def _index_remove(self, pk: Key, rec: Record, *, update_sorted: bool = True) -> None:
        schema = self.schema
        for u_dict, (u_get, has_null, is_single) in zip(self.uniques, schema.unique_extractors):
            uv = u_get(rec)
            if has_null and (uv is None if is_single else None in uv):
                continue
            if u_dict.get(uv) == pk:
                del u_dict[uv]
        for grp, idx_get in schema.index_extractors:
            _idx_remove(self.indexes[grp], idx_get(rec), pk)
        if update_sorted and self.sorted_indexes:
            for col, si in self.sorted_indexes.items():
                si.remove(rec[col], pk)

    def _find_unique_collisions(self, rec: Record, own_pk: Key | None = None) -> list[Key]:
        collisions: list[Key] = []
        for u_dict, (u_get, has_null, is_single) in zip(self.uniques, self.schema.unique_extractors):
            uv = u_get(rec)
            if has_null and (uv is None if is_single else None in uv):
                continue
            existing = u_dict.get(uv)
            if existing is not None and existing != own_pk:
                collisions.append(existing)
        return collisions

    def apply_put_many(self, records: list[Record], replace: bool) -> int:
        if not records:
            return 0
        schema = self.schema
        keyed = bool(schema.key_pos)
        bulk = self.sorted_indexes and len(records) > max(64, int(len(self.rows) * _BULK_INVALIDATE_RATIO))
        if bulk:
            self.sorted_indexes.clear()
        update_sorted = not bulk and bool(self.sorted_indexes)
        written = 0
        rows = self.rows
        idx_pairs = [(self.indexes[grp], idx_get) for grp, idx_get in schema.index_extractors]

        # Fast path 1: keyed store without unique constraints or active sorted indexes
        if keyed and not self.uniques and not update_sorted:
            key_get = schema.key_getter
            try:
                for rec in records:
                    pk = key_get(rec)  # type: ignore[misc]
                    old = rows.get(pk)
                    if old is not None:
                        if not replace:
                            continue
                        for idx_dict, idx_get in idx_pairs:
                            _idx_remove(idx_dict, idx_get(old), pk)
                    rows[pk] = rec
                    for idx_dict, idx_get in idx_pairs:
                        val = idx_get(rec)
                        cur = idx_dict.get(val)
                        if cur is None:
                            idx_dict[val] = pk
                        elif type(cur) is set:
                            cur.add(pk)
                        elif cur != pk:
                            idx_dict[val] = {cur, pk}
                    written += 1
            except TypeError as exc:
                raise CacheSchemaError(f"store {schema.name!r}: unhashable key or indexed value: {exc}") from exc
            return written

        # Fast path 2: keyless store without active sorted indexes (e.g. rel)
        if not keyed and not update_sorted:
            u_pairs = list(zip(self.uniques, schema.unique_extractors))
            next_rid = self.next_rid
            try:
                if len(u_pairs) == 1:
                    u_dict, (u_get, has_null, is_single) = u_pairs[0]
                    for rec in records:
                        uv = u_get(rec)
                        skip_u = has_null and (uv is None if is_single else None in uv)
                        if not skip_u:
                            existing = u_dict.get(uv)
                            if existing is not None:
                                if not replace:
                                    continue
                                coll_rec = rows.pop(existing, None)
                                if coll_rec is not None:
                                    self._index_remove(existing, coll_rec, update_sorted=False)
                                    self.deleted_since_compact += 1
                        pk = next_rid
                        next_rid += 1
                        rows[pk] = rec
                        if not skip_u:
                            u_dict[uv] = pk
                        for idx_dict, idx_get in idx_pairs:
                            val = idx_get(rec)
                            cur = idx_dict.get(val)
                            if cur is None:
                                idx_dict[val] = pk
                            elif type(cur) is set:
                                cur.add(pk)
                            elif cur != pk:
                                idx_dict[val] = {cur, pk}
                        written += 1
                else:
                    for rec in records:
                        u_vals: list[tuple[dict[Any, Key], Any]] = []
                        colls: list[Key] | None = None
                        for u_dict, (u_get, has_null, is_single) in u_pairs:
                            uv = u_get(rec)
                            if has_null and (uv is None if is_single else None in uv):
                                continue
                            existing = u_dict.get(uv)
                            if existing is not None:
                                if not replace:
                                    colls = [existing]
                                    break
                                if colls is None:
                                    colls = [existing]
                                else:
                                    colls.append(existing)
                            u_vals.append((u_dict, uv))
                        if not replace and colls is not None:
                            continue
                        if colls is not None:
                            for coll_pk in colls:
                                coll_rec = rows.pop(coll_pk, None)
                                if coll_rec is not None:
                                    self._index_remove(coll_pk, coll_rec, update_sorted=False)
                                    self.deleted_since_compact += 1
                        pk = next_rid
                        next_rid += 1
                        rows[pk] = rec
                        for u_dict, uv in u_vals:
                            u_dict[uv] = pk
                        for idx_dict, idx_get in idx_pairs:
                            val = idx_get(rec)
                            cur = idx_dict.get(val)
                            if cur is None:
                                idx_dict[val] = pk
                            elif type(cur) is set:
                                cur.add(pk)
                            elif cur != pk:
                                idx_dict[val] = {cur, pk}
                        written += 1
            except TypeError as exc:
                raise CacheSchemaError(f"store {schema.name!r}: unhashable key or indexed value: {exc}") from exc
            self.next_rid = next_rid
            return written

        for rec in records:
            if keyed:
                pk = schema.key_getter(rec)  # type: ignore[misc]
                old = rows.get(pk)
                if not replace:
                    if old is not None or self._find_unique_collisions(rec):
                        continue
                    rows[pk] = rec
                    self._index_add(pk, rec, update_sorted=update_sorted)
                    written += 1
                else:
                    if old is not None:
                        self._index_remove(pk, old, update_sorted=update_sorted)
                    for coll_pk in self._find_unique_collisions(rec, pk):
                        coll_rec = rows.pop(coll_pk, None)
                        if coll_rec is not None:
                            self._index_remove(coll_pk, coll_rec, update_sorted=update_sorted)
                    rows[pk] = rec
                    self._index_add(pk, rec, update_sorted=update_sorted)
                    written += 1
            else:
                colls = self._find_unique_collisions(rec)
                if not replace:
                    if colls:
                        continue
                    pk = self.next_rid
                    self.next_rid = pk + 1
                    rows[pk] = rec
                    self._index_add(pk, rec, update_sorted=update_sorted)
                    written += 1
                else:
                    for coll_pk in colls:
                        coll_rec = rows.pop(coll_pk, None)
                        if coll_rec is not None:
                            self._index_remove(coll_pk, coll_rec, update_sorted=update_sorted)
                    pk = self.next_rid
                    self.next_rid = pk + 1
                    rows[pk] = rec
                    self._index_add(pk, rec, update_sorted=update_sorted)
                    written += 1
        return written

    def apply_delete_keys(self, keys: Iterable[Key]) -> int:
        rows = self.rows
        if not rows:
            return 0
        key_list = list(keys)
        if not key_list:
            return 0
        bulk = self.sorted_indexes and len(key_list) > max(64, int(len(rows) * _BULK_INVALIDATE_RATIO))
        if bulk:
            self.sorted_indexes.clear()
        update_sorted = not bulk and bool(self.sorted_indexes)
        deleted = 0
        for k in key_list:
            old = rows.pop(k, None)
            if old is not None:
                self._index_remove(k, old, update_sorted=update_sorted)
                deleted += 1
        self.deleted_since_compact += deleted
        return deleted

    def apply_update_pks(self, matched_pks: list[Key], updates: dict[int, Any]) -> int:
        if not matched_pks or not updates:
            return 0
        rows = self.rows
        schema = self.schema
        upd_cols = set(updates)
        # Build new records first and check unique constraints for atomicity
        new_recs: list[tuple[Key, Record, Record]] = []
        if self.uniques:
            seen_uniques: list[dict[Any, Key]] = [{} for _ in schema.unique_groups]
            matched_set = set(matched_pks)
            for pk in matched_pks:
                old = rows[pk]
                lst = list(old)
                for pos, val in updates.items():
                    lst[pos] = val
                new_rec = tuple(lst)
                for u_i, (u_dict, (pos, has_null)) in enumerate(zip(self.uniques, schema.unique_groups)):
                    if len(pos) == 1:
                        uv = new_rec[pos[0]]
                        if has_null and uv is None:
                            continue
                    else:
                        uv = tuple(new_rec[p] for p in pos)
                        if has_null and None in uv:
                            continue
                    prev_in_batch = seen_uniques[u_i].get(uv)
                    if prev_in_batch is not None and prev_in_batch != pk:
                        raise CacheError(f"store {schema.name!r}: UNIQUE constraint failed")
                    existing = u_dict.get(uv)
                    if existing is not None and existing != pk and existing not in matched_set:
                        raise CacheError(f"store {schema.name!r}: UNIQUE constraint failed")
                    seen_uniques[u_i][uv] = pk
                new_recs.append((pk, old, new_rec))
        else:
            for pk in matched_pks:
                old = rows[pk]
                lst = list(old)
                for pos, val in updates.items():
                    lst[pos] = val
                new_recs.append((pk, old, tuple(lst)))

        bulk = self.sorted_indexes and len(new_recs) > max(64, int(len(rows) * _BULK_INVALIDATE_RATIO))
        if bulk:
            for col in list(self.sorted_indexes):
                if col in upd_cols:
                    del self.sorted_indexes[col]
        touched_uniques = [
            (u_dict, u_get, has_null, is_single)
            for u_dict, (pos, _), (u_get, has_null, is_single) in zip(
                self.uniques, schema.unique_groups, schema.unique_extractors)
            if upd_cols.intersection(pos)
        ]
        touched_indexes = [
            (self.indexes[grp], idx_get)
            for grp, idx_get in schema.index_extractors
            if upd_cols.intersection(grp)
        ]
        touched_sorted = [
            (col, si) for col, si in self.sorted_indexes.items() if col in upd_cols
        ] if not bulk else []

        for pk, old, new_rec in new_recs:
            for u_dict, u_get, has_null, is_single in touched_uniques:
                uv = u_get(old)
                if not (has_null and (uv is None if is_single else None in uv)):
                    if u_dict.get(uv) == pk:
                        del u_dict[uv]
            for idx_dict, idx_get in touched_indexes:
                _idx_remove(idx_dict, idx_get(old), pk)
            for col, si in touched_sorted:
                si.remove(old[col], pk)

            rows[pk] = new_rec

            for u_dict, u_get, has_null, is_single in touched_uniques:
                uv = u_get(new_rec)
                if not (has_null and (uv is None if is_single else None in uv)):
                    u_dict[uv] = pk
            for idx_dict, idx_get in touched_indexes:
                _idx_add(idx_dict, idx_get(new_rec), pk)
            for col, si in touched_sorted:
                si.add(new_rec[col], pk)
        return len(new_recs)

    def clear_all(self) -> None:
        self.rows.clear()
        self.next_rid = 0
        self.deleted_since_compact = 0
        for u in self.uniques:
            u.clear()
        for idx in self.indexes.values():
            idx.clear()
        self.sorted_indexes.clear()

    def compact(self) -> None:
        needs_sorted = any(si.needs_compact() for si in self.sorted_indexes.values())
        need_dicts = self.deleted_since_compact >= max(64, len(self.rows) // 8)
        if not need_dicts and not needs_sorted:
            return
        if need_dicts:
            self.rows = dict(self.rows)
            self.uniques = [dict(u) for u in self.uniques]
            self.indexes = {
                g: {k: (set(v) if type(v) is set else v) for k, v in idx.items()}
                for g, idx in self.indexes.items()
            }
            self.deleted_since_compact = 0
        for si in self.sorted_indexes.values():
            si.compact()


class _KeyedDelta:
    """O(delta) overlay for large simple keyed stores inside a ``batch()``."""

    __slots__ = ("base", "schema", "puts", "deletes")

    def __init__(self, base: _StoreState) -> None:
        self.base = base
        self.schema = base.schema
        self.puts: dict[Key, Record] = {}
        self.deletes: set[Key] = set()

    def get(self, key: Key) -> Record | None:
        if key in self.puts:
            return self.puts[key]
        if key in self.deletes:
            return None
        return self.base.rows.get(key)

    def get_many(self, keys: Iterable[Key]) -> dict[Key, Record]:
        out: dict[Key, Record] = {}
        puts = self.puts
        deletes = self.deletes
        base_rows = self.base.rows
        for k in keys:
            rec = puts.get(k)
            if rec is not None:
                out[k] = rec
            elif k not in deletes:
                rec = base_rows.get(k)
                if rec is not None:
                    out[k] = rec
        return out

    def put_many(self, records: list[Record], replace: bool) -> int:
        schema = self.schema
        puts = self.puts
        deletes = self.deletes
        base_rows = self.base.rows
        written = 0
        for rec in records:
            pk = schema.extract_key(rec)
            if not replace:
                if pk in puts or (pk not in deletes and pk in base_rows):
                    continue
            deletes.discard(pk)
            puts[pk] = rec
            written += 1
        return written

    def delete_many(self, keys: Iterable[Key]) -> int:
        puts = self.puts
        deletes = self.deletes
        base_rows = self.base.rows
        deleted = 0
        for k in keys:
            existed = k in puts or (k not in deletes and k in base_rows)
            if existed:
                puts.pop(k, None)
                deletes.add(k)
                deleted += 1
        return deleted

    def materialize(self) -> _StoreState:
        st = self.base.clone()
        if self.deletes:
            st.apply_delete_keys(self.deletes)
        if self.puts:
            st.apply_put_many(list(self.puts.values()), replace=True)
        return st

    def apply_to(self, target: _StoreState) -> None:
        if self.deletes:
            target.apply_delete_keys(self.deletes)
        if self.puts:
            target.apply_put_many(list(self.puts.values()), replace=True)


# ----------------------------------------------------------------------------------------- query planner & predicates

def _compile_predicates(conds: list[tuple[int, Condition]]) -> Callable[[Record], bool] | None:
    """Compile residual conditions into a fast predicate, or None if no conditions remain."""
    if not conds:
        return None
    checks: list[Callable[[Record], bool]] = []
    for pos, c in conds:
        if isinstance(c, Eq):
            v = c.value
            if v is None:
                checks.append(lambda r, p=pos: r[p] is None)
            else:
                checks.append(lambda r, p=pos, v=v: r[p] == v)
        elif isinstance(c, In):
            vals = frozenset(v for v in c.values if v is not None)
            if not vals:
                return lambda r: False
            checks.append(lambda r, p=pos, vs=vals: r[p] in vs)
        elif isinstance(c, Range):
            ge, gt, le, lt = c.ge, c.gt, c.le, c.lt
            if ge is not None and gt is not None:
                if gt >= ge:
                    ge = None
                else:
                    gt = None
            if le is not None and lt is not None:
                if lt <= le:
                    le = None
                else:
                    lt = None
            def _range_chk(r: Record, p: int = pos, ge=ge, gt=gt, le=le, lt=lt) -> bool:
                v = r[p]
                if v is None:
                    return False
                if ge is not None and v < ge:
                    return False
                if gt is not None and v <= gt:
                    return False
                if le is not None and v > le:
                    return False
                if lt is not None and v >= lt:
                    return False
                return True
            checks.append(_range_chk)
        elif isinstance(c, Prefix):
            pfx = _normalize_prefix(c.value)
            checks.append(lambda r, p=pos, pfx=pfx, tp=type(pfx): isinstance((v := r[p]), tp) and v.startswith(pfx))
        elif isinstance(c, IsNull):
            if c.negate:
                checks.append(lambda r, p=pos: r[p] is not None)
            else:
                checks.append(lambda r, p=pos: r[p] is None)
    if len(checks) == 1:
        return checks[0]
    if len(checks) == 2:
        c0, c1 = checks
        return lambda r: c0(r) and c1(r)
    if len(checks) == 3:
        c0, c1, c2 = checks
        return lambda r: c0(r) and c1(r) and c2(r)
    return lambda r: all(chk(r) for chk in checks)


def _validate_and_normalize_conds(schema: _StoreSchema, where: Where, single_call: bool) -> list[tuple[int, Condition]]:
    conds = as_conditions(where)
    out: list[tuple[int, Condition]] = []
    big_in = 0
    for c in conds:
        pos = schema.check_field(c.field)
        if isinstance(c, Prefix):
            _normalize_prefix(c.value)
        elif isinstance(c, In):
            if len(c.values) > IN_CHUNK:
                big_in += 1
        elif isinstance(c, Range):
            if c.ge is None and c.gt is None and c.le is None and c.lt is None:
                continue
        elif not isinstance(c, (Eq, IsNull)):
            raise CacheSchemaError(f"unknown condition: {c!r}")
        out.append((pos, c))
    if big_in > 1 or (big_in > 0 and single_call):
        raise CacheSchemaError(
            f"an In list longer than {IN_CHUNK} cannot be combined with order_by/limit or another long In")
    return out


def _get_sorted_index(state: _StoreState, col: int, lock: threading.Lock) -> _SortedIndex:
    si = state.sorted_indexes.get(col)
    if si is not None:
        return si
    built = _SortedIndex.build(state.rows, col)
    with lock:
        return state.sorted_indexes.setdefault(col, built)


def _plan_candidates(
    state: _StoreState,
    conds: list[tuple[int, Condition]],
    order_specs: list[tuple[int, bool]],
    limit: int | None,
    lock: threading.Lock,
) -> tuple[Iterable[tuple[Key, Record]], list[tuple[int, Condition]], bool]:
    """Select the narrowest index for ``conds``.

    Returns ``(candidate_pairs, residual_conds, already_ordered_by_first)``.
    """
    schema = state.schema
    rows = state.rows
    if not rows:
        return (), [], False

    # Short-circuit impossible conditions
    for i, (pos, c) in enumerate(conds):
        if isinstance(c, In):
            if not any(v is not None for v in c.values):
                return (), [], False
        elif isinstance(c, Eq) and c.value is None and not schema.field_nullable[pos]:
            return (), [], False
        elif isinstance(c, IsNull) and not c.negate and not schema.field_nullable[pos]:
            return (), [], False

    # Map field pos -> list of (cond_index, Condition)
    by_pos: dict[int, list[tuple[int, Condition]]] = {}
    for i, (pos, c) in enumerate(conds):
        by_pos.setdefault(pos, []).append((i, c))

    # 1. Full PK Eq
    if schema.key_pos:
        pk_vals: list[Any] = []
        pk_cond_idxs: set[int] = set()
        for p in schema.key_pos:
            matched = None
            for ci, c in by_pos.get(p, ()):
                if isinstance(c, Eq) and c.value is not None:
                    matched = (ci, c.value)
                    break
            if matched is None:
                break
            pk_cond_idxs.add(matched[0])
            pk_vals.append(matched[1])
        else:
            pk = pk_vals[0] if schema.key_single is not None else tuple(pk_vals)
            rec = rows.get(pk)
            res = [conds[i] for i in range(len(conds)) if i not in pk_cond_idxs]
            return (((pk, rec),) if rec is not None else ()), res, False

    # 2. Unique group Eq
    for u_dict, (u_pos, _) in zip(state.uniques, schema.unique_groups):
        u_vals: list[Any] = []
        u_cond_idxs: set[int] = set()
        for p in u_pos:
            matched = None
            for ci, c in by_pos.get(p, ()):
                if isinstance(c, Eq) and c.value is not None:
                    matched = (ci, c.value)
                    break
            if matched is None:
                break
            u_cond_idxs.add(matched[0])
            u_vals.append(matched[1])
        else:
            uv = u_vals[0] if len(u_pos) == 1 else tuple(u_vals)
            pk = u_dict.get(uv)
            res = [conds[i] for i in range(len(conds)) if i not in u_cond_idxs]
            if pk is None:
                return (), res, False
            rec = rows.get(pk)
            return (((pk, rec),) if rec is not None else ()), res, False

    # 3. PK In / composite Eq + In on PK
    if schema.key_pos:
        if schema.key_single is not None:
            for ci, c in by_pos.get(schema.key_single, ()):
                if isinstance(c, In):
                    seen_k: set[Any] = set()
                    pairs: list[tuple[Key, Record]] = []
                    for v in c.values:
                        if v is not None and v not in seen_k:
                            seen_k.add(v)
                            rec = rows.get(v)
                            if rec is not None:
                                pairs.append((v, rec))
                    res = [conds[i] for i in range(len(conds)) if i != ci]
                    return pairs, res, False
        else:
            # Composite PK: all key fields must be Eq or In, with at most one In
            in_slot = -1
            in_ci = -1
            in_vals: list[Any] = []
            eq_vals: list[Any] = [None] * len(schema.key_pos)
            used_cis: set[int] = set()
            ok = True
            for slot, p in enumerate(schema.key_pos):
                found = False
                for ci, c in by_pos.get(p, ()):
                    if isinstance(c, Eq) and c.value is not None:
                        eq_vals[slot] = c.value
                        used_cis.add(ci)
                        found = True
                        break
                if found:
                    continue
                if in_slot == -1:
                    for ci, c in by_pos.get(p, ()):
                        if isinstance(c, In):
                            in_slot = slot
                            in_ci = ci
                            in_vals = list(dict.fromkeys(v for v in c.values if v is not None))
                            used_cis.add(ci)
                            found = True
                            break
                if not found:
                    ok = False
                    break
            if ok and in_slot != -1:
                pairs = []
                for v in in_vals:
                    eq_vals[in_slot] = v
                    pk = tuple(eq_vals)
                    rec = rows.get(pk)
                    if rec is not None:
                        pairs.append((pk, rec))
                res = [conds[i] for i in range(len(conds)) if i not in used_cis]
                return pairs, res, False

    # 4. Smallest hash index Eq / IsNull(negate=False)
    best_hash: tuple[int, Any, dict[Any, Any], set[int]] | None = None
    for grp, idx in state.indexes.items():
        g_vals: list[Any] = []
        g_cis: set[int] = set()
        for p in grp:
            matched = None
            for ci, c in by_pos.get(p, ()):
                if isinstance(c, Eq):
                    matched = (ci, c.value)
                    break
                if isinstance(c, IsNull) and not c.negate:
                    matched = (ci, None)
                    break
            if matched is None:
                break
            g_cis.add(matched[0])
            g_vals.append(matched[1])
        else:
            lookup_v = g_vals[0] if len(grp) == 1 else tuple(g_vals)
            cnt = _idx_len(idx, lookup_v)
            if cnt == 0:
                return (), [], False
            if best_hash is None or cnt < best_hash[0]:
                best_hash = (cnt, lookup_v, idx, g_cis)

    if best_hash is not None:
        _, lookup_v, idx, g_cis = best_hash
        res = [conds[i] for i in range(len(conds)) if i not in g_cis]
        pairs_iter = ((pk, rows[pk]) for pk in _idx_iter(idx, lookup_v))
        return pairs_iter, res, False

    # 5. Hash index In (1-col)
    best_in: tuple[int, list[Any], dict[Any, Any], int] | None = None
    for grp, idx in state.indexes.items():
        if len(grp) != 1:
            continue
        for ci, c in by_pos.get(grp[0], ()):
            if isinstance(c, In):
                dedup = list(dict.fromkeys(v for v in c.values if v is not None))
                est = sum(_idx_len(idx, v) for v in dedup)
                if best_in is None or est < best_in[0]:
                    best_in = (est, dedup, idx, ci)
    if best_in is not None:
        _, dedup, idx, ci = best_in
        res = [conds[i] for i in range(len(conds)) if i != ci]
        def _in_gen() -> Iterator[tuple[Key, Record]]:
            for v in dedup:
                for pk in _idx_iter(idx, v):
                    yield pk, rows[pk]
        return _in_gen(), res, False

    # 6. SortedIndex Range / Prefix
    for i, (pos, c) in enumerate(conds):
        if isinstance(c, Range):
            si = _get_sorted_index(state, pos, lock)
            res = [conds[j] for j in range(len(conds)) if j != i]
            already = len(order_specs) == 1 and order_specs[0] == (pos, False)
            return ((pk, rows[pk]) for pk in si.range_pks(c.ge, c.gt, c.le, c.lt)), res, already
        if isinstance(c, Prefix):
            pfx = _normalize_prefix(c.value)
            expected_type = FieldType.TEXT if isinstance(pfx, str) else FieldType.BYTES
            if schema.field_types[pos] != expected_type:
                return (), [], False
            si = _get_sorted_index(state, pos, lock)
            lo, hi = _prefix_bounds(pfx)
            res = [conds[j] for j in range(len(conds)) if j != i]
            return (
                ((pk, rec) for pk in si.range_pks(ge=lo, lt=hi) if (rec := rows[pk])[pos].startswith(pfx)),
                res,
                False,
            )

    # 7. SortedIndex for order_by when limit is set and single order_by column
    if order_specs and limit is not None and limit > 0 and len(order_specs) == 1:
        ord_pos, ord_desc = order_specs[0]
        si = _get_sorted_index(state, ord_pos, lock)
        return ((pk, rows[pk]) for pk in si.iter_order(ord_desc)), conds, True

    # 8. Fallback full scan
    return rows.items(), conds, False


def _sort_records(records: list[Record], order_specs: list[tuple[int, bool]],
                  nullable: tuple[bool, ...], limit: int | None) -> list[Record]:
    if not records or not order_specs:
        return records
    if len(order_specs) == 1 and limit is not None and 0 < limit < len(records) and len(records) > 256:
        col, desc = order_specs[0]
        if nullable[col]:
            nones = [r for r in records if r[col] is None]
            non_none = [r for r in records if r[col] is not None]
        else:
            nones = []
            non_none = records
        key_fn = operator.itemgetter(col)
        if desc:
            top = heapq.nlargest(limit, non_none, key=key_fn)
            if len(top) < limit and nones:
                top.extend(nones[:limit - len(top)])
            return top
        else:
            out = nones[:limit]
            if len(out) < limit:
                out.extend(heapq.nsmallest(limit - len(out), non_none, key=key_fn))
            return out

    for col, desc in reversed(order_specs):
        if nullable[col]:
            nones = [r for r in records if r[col] is None]
            non_none = [r for r in records if r[col] is not None]
            if non_none:
                non_none.sort(key=operator.itemgetter(col), reverse=desc)
            records = (non_none + nones) if desc else (nones + non_none)
        else:
            records.sort(key=operator.itemgetter(col), reverse=desc)
    return records


def _compile_projector(col_indices: Sequence[int] | None) -> Callable[[Record], tuple] | None:
    if not col_indices:
        return None
    if len(col_indices) == 1:
        idx = col_indices[0]
        return lambda r, i=idx: (r[i],)
    return operator.itemgetter(*col_indices)


# ----------------------------------------------------------------------------------------- space / store / engine

class _SpaceStorage:
    """Backing storage for one ``(namespace, name)`` space owned by ``RAMCacheEngine``."""

    __slots__ = ("specs", "schemas", "stores", "meta")

    def __init__(self, specs: tuple[StoreSpec, ...]) -> None:
        self.specs = specs
        self.schemas = {s.name: _StoreSchema(s) for s in specs}
        self.stores = {s.name: _StoreState(self.schemas[s.name]) for s in specs}
        self.meta: dict[str, str] = {}


class _BatchTx:
    """Thread-local uncommitted state for a ``batch()`` block."""

    __slots__ = ("storage", "working", "deltas", "meta", "meta_cleared")

    def __init__(self, storage: _SpaceStorage) -> None:
        self.storage = storage
        self.working: dict[str, _StoreState] = {}
        self.deltas: dict[str, _KeyedDelta] = {}
        self.meta: dict[str, str] | None = None
        self.meta_cleared: bool = False

    def get_write_target(self, name: str, *, allow_delta: bool) -> _StoreState | _KeyedDelta:
        w = self.working.get(name)
        if w is not None:
            return w
        d = self.deltas.get(name)
        if d is not None:
            if allow_delta:
                return d
            w = d.materialize()
            del self.deltas[name]
            self.working[name] = w
            return w
        base = self.storage.stores[name]
        if allow_delta and base.schema.simple_keyed and len(base.rows) > _BATCH_DELTA_THRESHOLD:
            d = _KeyedDelta(base)
            self.deltas[name] = d
            return d
        w = base.clone()
        self.working[name] = w
        return w

    def get_read_state(self, name: str) -> _StoreState:
        w = self.working.get(name)
        if w is not None:
            return w
        d = self.deltas.get(name)
        if d is not None:
            w = d.materialize()
            del self.deltas[name]
            self.working[name] = w
            return w
        return self.storage.stores[name]


class _PinnedIter:
    """Iterator wrapper that releases ``st.readers`` deterministically on exhaust, close, or GC."""

    __slots__ = ("_it", "_store", "_st", "_pinned")

    def __init__(self, it: Iterator[tuple], store: "_RAMStore", st: _StoreState, pinned: bool) -> None:
        self._it = it
        self._store = store
        self._st = st
        self._pinned = pinned

    def __iter__(self) -> "_PinnedIter":
        return self

    def __next__(self) -> tuple:
        try:
            return next(self._it)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._pinned:
            self._pinned = False
            self._store._end_read(self._st, True)

    def __del__(self) -> None:
        self.close()


class _RAMStore(CacheStore):
    def __init__(self, space: "_RAMSpace", schema: _StoreSchema) -> None:
        self.space = space
        self.spec = schema.spec
        self._schema = schema

    def _check_open(self) -> None:
        if self.space._closed:
            raise CacheError(f"cache space {self.space.namespace}/{self.space.name} is closed")

    def _begin_read(self) -> tuple[_StoreState, bool]:
        """Return ``(state, pinned)``. Caller must call ``_end_read(state, pinned)`` in ``finally``."""
        self._check_open()
        tx = self.space._current_tx()
        if tx is not None:
            return tx.get_read_state(self.spec.name), False
        with self.space._commit_lock:
            st = self.space._storage.stores[self.spec.name]
            st.readers += 1
            return st, True

    def _end_read(self, st: _StoreState, pinned: bool) -> None:
        if pinned:
            with self.space._commit_lock:
                st.readers -= 1

    def _mutate(self, fn: Callable[[_StoreState], Any]) -> Any:
        self._check_open()
        name = self.spec.name
        with self.space._wlock:
            tx = self.space._current_tx()
            if tx is not None:
                target = tx.get_write_target(name, allow_delta=False)
                return fn(target)
            with self.space._commit_lock:
                st = self.space._storage.stores[name]
                if st.readers == 0:
                    return fn(st)
            cloned = st.clone()
            res = fn(cloned)
            with self.space._commit_lock:
                self.space._storage.stores[name] = cloned
            return res

    # ------------------------------------------------------------------ reads
    def get(self, key: Key) -> Record | None:
        self._check_open()
        k = self._schema.validate_key(key)
        tx = self.space._current_tx()
        if tx is not None:
            d = tx.deltas.get(self.spec.name)
            if d is not None:
                return d.get(k)
            w = tx.working.get(self.spec.name)
            if w is not None:
                return w.rows.get(k)
        with self.space._commit_lock:
            return self.space._storage.stores[self.spec.name].rows.get(k)

    def get_many(self, keys: Iterable[Key]) -> dict[Key, Record]:
        self._check_open()
        key_list = [self._schema.validate_key(k) for k in keys]
        if not key_list:
            return {}
        tx = self.space._current_tx()
        if tx is not None:
            d = tx.deltas.get(self.spec.name)
            if d is not None:
                return d.get_many(key_list)
        st, pinned = self._begin_read()
        try:
            rows = st.rows
            out: dict[Key, Record] = {}
            for k in key_list:
                rec = rows.get(k)
                if rec is not None:
                    out[k] = rec
            return out
        finally:
            self._end_read(st, pinned)

    def scan(self, where: Where = None, *, columns: Sequence[str] | None = None,
             order_by: Sequence[str] = (), limit: int | None = None) -> Iterator[tuple]:
        self._check_open()
        conds = _validate_and_normalize_conds(self._schema, where, single_call=bool(order_by) or limit is not None)
        col_indices = [self._schema.check_field(c) for c in columns] if columns else None
        projector = _compile_projector(col_indices)
        order_specs: list[tuple[int, bool]] = []
        for o in order_by:
            desc = o.startswith("-")
            fname = o[1:] if desc else o
            order_specs.append((self._schema.check_field(fname), desc))
        lim = int(limit) if limit is not None else None
        if lim == 0:
            return iter(())

        st, pinned = self._begin_read()
        if len(conds) == 1 and isinstance(conds[0][1], Eq) and not order_specs and lim is None:
            pos, eq = conds[0]
            val = eq.value
            if val is not None and self._schema.key_single == pos:
                return _PinnedIter(self._scan_pk_eq(st, val, projector), self, st, pinned)
            idx = st.indexes.get((pos,))
            if idx is not None:
                return _PinnedIter(self._scan_hash_eq(st, idx, val, projector), self, st, pinned)
        return _PinnedIter(self._scan_iter(st, conds, order_specs, lim, projector), self, st, pinned)

    def _scan_pk_eq(self, st: _StoreState, val: Any, projector: Callable[[Record], tuple] | None) -> Iterator[tuple]:
        rec = st.rows.get(val)
        if rec is not None:
            yield projector(rec) if projector is not None else rec

    def _scan_hash_eq(self, st: _StoreState, idx: dict[Any, Any], val: Any,
                      projector: Callable[[Record], tuple] | None) -> Iterator[tuple]:
        rows = st.rows
        cur = idx.get(val)
        if cur is None:
            return
        if type(cur) is set:
            if projector is None:
                for pk in cur:
                    yield rows[pk]
            else:
                for pk in cur:
                    yield projector(rows[pk])
        else:
            rec = rows[cur]
            yield projector(rec) if projector is not None else rec

    def _scan_iter(self, st: _StoreState, conds: list[tuple[int, Condition]],
                   order_specs: list[tuple[int, bool]], lim: int | None,
                   projector: Callable[[Record], tuple] | None) -> Iterator[tuple]:
        candidates, residual, already_ordered = _plan_candidates(
            st, conds, order_specs, lim, self.space._commit_lock)
        pred = _compile_predicates(residual)

        if order_specs and not already_ordered:
            if pred is None:
                matched = [rec for _, rec in candidates]
            else:
                matched = [rec for _, rec in candidates if pred(rec)]
            sorted_recs = _sort_records(matched, order_specs, self._schema.field_nullable, lim)
            if lim is not None and lim > 0:
                sorted_recs = sorted_recs[:lim]
            if projector is None:
                yield from sorted_recs
            else:
                for rec in sorted_recs:
                    yield projector(rec)
            return

        count = 0
        for _, rec in candidates:
            if pred is not None and not pred(rec):
                continue
            yield projector(rec) if projector is not None else rec
            if lim is not None and lim > 0:
                count += 1
                if count >= lim:
                    return

    def count(self, where: Where = None) -> int:
        self._check_open()
        conds = _validate_and_normalize_conds(self._schema, where, single_call=False)
        st, pinned = self._begin_read()
        try:
            if not conds:
                return len(st.rows)
            if len(conds) == 1:
                pos, c = conds[0]
                if isinstance(c, Eq):
                    if c.value is None:
                        if not self._schema.field_nullable[pos]:
                            return 0
                        idx = st.indexes.get((pos,))
                        if idx is not None:
                            return _idx_len(idx, None)
                    else:
                        if self._schema.key_single == pos:
                            return 1 if c.value in st.rows else 0
                        idx = st.indexes.get((pos,))
                        if idx is not None:
                            return _idx_len(idx, c.value)
                elif isinstance(c, IsNull) and not c.negate:
                    if not self._schema.field_nullable[pos]:
                        return 0
                    idx = st.indexes.get((pos,))
                    if idx is not None:
                        return _idx_len(idx, None)
                elif isinstance(c, Range):
                    si = _get_sorted_index(st, pos, self.space._commit_lock)
                    return si.range_count(c.ge, c.gt, c.le, c.lt)
            candidates, residual, _ = _plan_candidates(st, conds, [], None, self.space._commit_lock)
            pred = _compile_predicates(residual)
            if pred is None:
                return sum(1 for _ in candidates)
            return sum(1 for _, rec in candidates if pred(rec))
        finally:
            self._end_read(st, pinned)

    def sum(self, field: str, where: Where = None) -> int | float:
        self._check_open()
        pos = self._schema.check_field(field)
        conds = _validate_and_normalize_conds(self._schema, where, single_call=False)
        st, pinned = self._begin_read()
        try:
            if not conds:
                if not st.rows:
                    return 0
                getter = operator.itemgetter(pos)
                if not self._schema.field_nullable[pos]:
                    return sum(map(getter, st.rows.values()))
                return sum(v for v in map(getter, st.rows.values()) if v is not None)
            candidates, residual, _ = _plan_candidates(st, conds, [], None, self.space._commit_lock)
            pred = _compile_predicates(residual)
            total: int | float = 0
            for _, rec in candidates:
                if pred is not None and not pred(rec):
                    continue
                v = rec[pos]
                if v is not None:
                    total += v
            return total
        finally:
            self._end_read(st, pinned)

    def max(self, field: str, where: Where = None) -> Any:
        self._check_open()
        pos = self._schema.check_field(field)
        conds = _validate_and_normalize_conds(self._schema, where, single_call=False)
        st, pinned = self._begin_read()
        try:
            if not conds:
                if not st.rows:
                    return None
                si = st.sorted_indexes.get(pos)
                if si is not None:
                    return si._vals[-1][-1] if si._vals else None
                getter = operator.itemgetter(pos)
                if not self._schema.field_nullable[pos]:
                    return max(map(getter, st.rows.values()))
                return max((v for v in map(getter, st.rows.values()) if v is not None), default=None)
            candidates, residual, _ = _plan_candidates(st, conds, [], None, self.space._commit_lock)
            pred = _compile_predicates(residual)
            best: Any = None
            for _, rec in candidates:
                if pred is not None and not pred(rec):
                    continue
                v = rec[pos]
                if v is not None and (best is None or v > best):
                    best = v
            return best
        finally:
            self._end_read(st, pinned)

    # ------------------------------------------------------------------ writes
    def put(self, record: Record) -> None:
        self.put_many((record,))

    def put_many(self, records: Iterable[Record], *, replace: bool = True) -> int:
        self._check_open()
        schema = self._schema
        n_fields = schema.n_fields
        non_null = schema.non_null_pos
        validated: list[Record] = []
        append = validated.append
        for r in records:
            if type(r) is tuple:
                out = r
            elif isinstance(r, list):
                out = tuple(r)
            else:
                raise CacheSchemaError(f"store {schema.name!r}: record must be a tuple, got {type(r).__name__}")
            if len(out) != n_fields:
                raise CacheSchemaError(
                    f"store {schema.name!r}: expected {n_fields} fields, got {len(out)}")
            for i in non_null:
                if out[i] is None:
                    raise CacheSchemaError(
                        f"store {schema.name!r}: field {schema.field_names[i]!r} is not nullable")
            append(out)
        if not validated:
            return 0
        name = self.spec.name
        with self.space._wlock:
            tx = self.space._current_tx()
            if tx is not None:
                target = tx.get_write_target(name, allow_delta=True)
                if isinstance(target, _KeyedDelta):
                    return target.put_many(validated, replace)
                return target.apply_put_many(validated, replace)
            with self.space._commit_lock:
                st = self.space._storage.stores[name]
                if st.readers == 0:
                    return st.apply_put_many(validated, replace)
            cloned = st.clone()
            n = cloned.apply_put_many(validated, replace)
            with self.space._commit_lock:
                self.space._storage.stores[name] = cloned
            return n

    def delete(self, key: Key) -> bool:
        return self.delete_many((key,)) > 0

    def delete_many(self, keys: Iterable[Key]) -> int:
        self._check_open()
        key_list = [self._schema.validate_key(k) for k in keys]
        if not key_list:
            return 0
        name = self.spec.name
        with self.space._wlock:
            tx = self.space._current_tx()
            if tx is not None:
                target = tx.get_write_target(name, allow_delta=True)
                if isinstance(target, _KeyedDelta):
                    return target.delete_many(key_list)
                return target.apply_delete_keys(key_list)
            with self.space._commit_lock:
                st = self.space._storage.stores[name]
                if st.readers == 0:
                    return st.apply_delete_keys(key_list)
            cloned = st.clone()
            n = cloned.apply_delete_keys(key_list)
            with self.space._commit_lock:
                self.space._storage.stores[name] = cloned
            return n

    def delete_where(self, where: Where) -> int:
        self._check_open()
        conds = _validate_and_normalize_conds(self._schema, where, single_call=False)

        def _do(st: _StoreState) -> int:
            if not conds:
                n = len(st.rows)
                st.clear_all()
                return n
            candidates, residual, _ = _plan_candidates(st, conds, [], None, self.space._commit_lock)
            pred = _compile_predicates(residual)
            if pred is None:
                pks = [pk for pk, _ in candidates]
            else:
                pks = [pk for pk, rec in candidates if pred(rec)]
            return st.apply_delete_keys(pks)

        return self._mutate(_do)

    def update_where(self, where: Where, values: Mapping[str, Any]) -> int:
        self._check_open()
        if not values:
            return 0
        updates: dict[int, Any] = {}
        for name, val in values.items():
            pos = self._schema.check_field(name)
            if name in self.spec.key:
                raise CacheSchemaError(f"cannot update key field {name!r}")
            if val is None and not self._schema.field_nullable[pos]:
                raise CacheSchemaError(f"store {self.spec.name!r}: field {name!r} is not nullable")
            updates[pos] = val
        conds = _validate_and_normalize_conds(self._schema, where, single_call=False)

        def _do(st: _StoreState) -> int:
            candidates, residual, _ = _plan_candidates(st, conds, [], None, self.space._commit_lock)
            pred = _compile_predicates(residual)
            if pred is None:
                pks = [pk for pk, _ in candidates]
            else:
                pks = [pk for pk, rec in candidates if pred(rec)]
            return st.apply_update_pks(pks, updates)

        return self._mutate(_do)

    def clear(self) -> None:
        self._check_open()
        name = self.spec.name
        with self.space._wlock:
            tx = self.space._current_tx()
            if tx is not None:
                tx.deltas.pop(name, None)
                w = tx.working.get(name)
                if w is not None:
                    w.clear_all()
                else:
                    tx.working[name] = _StoreState(self._schema)
                return
            with self.space._commit_lock:
                st = self.space._storage.stores[name]
                if st.readers == 0:
                    st.clear_all()
                    return
            fresh = _StoreState(self._schema)
            with self.space._commit_lock:
                self.space._storage.stores[name] = fresh


class _RAMSpace(CacheSpace):
    def __init__(
        self,
        engine: "RAMCacheEngine",
        namespace: str,
        name: str,
        storage: _SpaceStorage,
        scope: str = "",
    ) -> None:
        self.engine = engine
        self.namespace = namespace
        self.name = name
        self._scope = scope
        self._storage = storage
        self.specs = storage.specs
        self._wlock = threading.RLock()
        self._commit_lock = threading.Lock()
        self._tls = threading.local()
        self._closed = False
        self._stores = {s.name: _RAMStore(self, storage.schemas[s.name]) for s in storage.specs}

    def _check_open(self) -> None:
        if self._closed:
            raise CacheError(f"cache space {self.namespace}/{self.name} is closed")

    def _current_tx(self) -> _BatchTx | None:
        return getattr(self._tls, "tx", None)

    @contextlib.contextmanager
    def batch(self) -> Iterator[None]:
        self._check_open()
        with self._wlock:
            depth = getattr(self._tls, "depth", 0)
            if depth == 0:
                self._tls.tx = _BatchTx(self._storage)
            self._tls.depth = depth + 1
            try:
                yield
            except BaseException:
                self._tls.depth -= 1
                if self._tls.depth == 0:
                    self._tls.tx = None
                raise
            self._tls.depth -= 1
            if self._tls.depth == 0:
                tx: _BatchTx = self._tls.tx
                self._tls.tx = None
                self._commit_tx(tx)

    def _commit_tx(self, tx: _BatchTx) -> None:
        # Prepare any delta targets that have active readers outside commit_lock if needed
        with self._commit_lock:
            for name, w in tx.working.items():
                self._storage.stores[name] = w
            for name, d in tx.deltas.items():
                base = self._storage.stores[name]
                if base.readers == 0:
                    d.apply_to(base)
                else:
                    cloned = base.clone()
                    d.apply_to(cloned)
                    self._storage.stores[name] = cloned
            if tx.meta is not None:
                self._storage.meta = tx.meta
            elif tx.meta_cleared:
                self._storage.meta.clear()

    def store(self, name: str) -> CacheStore:
        self._check_open()
        try:
            return self._stores[name]
        except KeyError:
            raise CacheSchemaError(f"cache space {self.namespace}/{self.name} declares no store {name!r}") from None

    def get_meta(self, key: str) -> str | None:
        self._check_open()
        tx = self._current_tx()
        if tx is not None:
            if tx.meta is not None:
                return tx.meta.get(key)
            if tx.meta_cleared:
                return None
        with self._commit_lock:
            return self._storage.meta.get(key)

    def set_meta(self, key: str, value: str) -> None:
        self._check_open()
        with self._wlock:
            tx = self._current_tx()
            if tx is not None:
                if tx.meta is None:
                    tx.meta = {} if tx.meta_cleared else dict(self._storage.meta)
                tx.meta[key] = str(value)
                return
            with self._commit_lock:
                self._storage.meta[key] = str(value)

    def delete_meta(self, key: str) -> None:
        self._check_open()
        with self._wlock:
            tx = self._current_tx()
            if tx is not None:
                if tx.meta is None:
                    tx.meta = {} if tx.meta_cleared else dict(self._storage.meta)
                tx.meta.pop(key, None)
                return
            with self._commit_lock:
                self._storage.meta.pop(key, None)

    def clear(self) -> None:
        self._check_open()
        with self._wlock:
            tx = self._current_tx()
            if tx is not None:
                tx.deltas.clear()
                for name, schema in self._storage.schemas.items():
                    tx.working[name] = _StoreState(schema)
                tx.meta = {}
                tx.meta_cleared = True
                return
            with self._commit_lock:
                for name, st in self._storage.stores.items():
                    if st.readers == 0:
                        st.clear_all()
                    else:
                        self._storage.stores[name] = _StoreState(st.schema)
                self._storage.meta.clear()

    def compact(self) -> None:
        self._check_open()
        with self._wlock:
            with self._commit_lock:
                for name, st in self._storage.stores.items():
                    if st.readers == 0:
                        st.compact()
                    else:
                        cloned = st.clone()
                        cloned.compact()
                        self._storage.stores[name] = cloned

    def size_bytes(self) -> int:
        self._check_open()
        with self._commit_lock:
            total = len(self._storage.meta) * 96
            for st in self._storage.stores.values():
                n = len(st.rows)
                if n > 0:
                    per_row = 64 + 32 * st.schema.n_fields + 32 * len(st.indexes) + 32 * len(st.sorted_indexes)
                    total += n * per_row
            return total

    def close(self) -> None:
        self._closed = True
        self.engine._forget(self)

    def discard(self) -> None:
        self.close()
        self.engine._discard_backing(self._scope, self.namespace, self.name)

    @property
    def location(self) -> str:
        return self.engine.location(self.namespace, self.name)


class RAMCacheEngine(BaseCacheEngine):
    """``cache.engine = "ram"`` (aliases ``memory``, ``mem``, ``in-memory``, ``in_memory``); pure-Python in-RAM cache engine.

    ``root`` scopes spaces in process memory; by default it is resolved on every open via
    ``cache_engine.cache_root()``. Spaces persist in memory across ``close()``/``open()`` until ``discard()``.
    """

    name = "ram"

    def __init__(self, root: Path | str | None = None) -> None:
        self._root = Path(root) if root is not None else None
        self._backing: dict[tuple[str, str, str], _SpaceStorage] = {}
        self._spaces: dict[tuple[str, str, str], _RAMSpace] = {}
        self._lock = threading.Lock()

    def _scope(self) -> str:
        if self._root is not None:
            return str(self._root)
        from . import cache_root
        return str(cache_root())

    def open(self, namespace: str, name: str, stores: Sequence[StoreSpec]) -> CacheSpace:
        check_namespace(namespace)
        specs = tuple(stores)
        scope = self._scope()
        key = (scope, namespace, name)
        with self._lock:
            space = self._spaces.get(key)
            if space is not None:
                if space.specs != specs:
                    raise CacheSchemaError(f"cache space {namespace}/{name} is already open with another schema")
                return space
            storage = self._backing.get(key)
            if storage is not None:
                if storage.specs != specs:
                    raise CacheSchemaError(f"cache space {namespace}/{name} holds a different schema")
            else:
                storage = _SpaceStorage(specs)
                self._backing[key] = storage
            space = _RAMSpace(self, namespace, name, storage, scope=scope)
            self._spaces[key] = space
            return space

    def exists(self, namespace: str, name: str) -> bool:
        check_namespace(namespace)
        scope = self._scope()
        with self._lock:
            return (scope, namespace, name) in self._backing

    def location(self, namespace: str, name: str) -> str:
        check_namespace(namespace)
        return f"ram://{namespace}/{name}"

    def _forget(self, space: _RAMSpace) -> None:
        key = (space._scope, space.namespace, space.name)
        with self._lock:
            if self._spaces.get(key) is space:
                del self._spaces[key]

    def _discard_backing(self, scope: str, namespace: str, name: str) -> None:
        with self._lock:
            self._backing.pop((scope, namespace, name), None)

    def close(self) -> None:
        with self._lock:
            spaces = list(self._spaces.values())
        for space in spaces:
            space.close()
