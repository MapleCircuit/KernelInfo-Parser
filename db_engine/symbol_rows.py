"""db_engine/symbol_rows.py - Symbol references and definitions of target ASTs, read through query specs.

The `m_symbol_ref` / `m_symbol_def` views (`db_engine/views.py`) combine per-blob rows with rows shared by every
blob that places a tag. Filtering such a view by `ast_id` joins each blob using a target with every tag of every
blob holding the key (seconds to minutes for a common function), so these readers go key first:

1. the target's symbol keys (`m_blob_symbol.target_ast_id IN targets`),
2. the tag rows of those keys, their placements (`m_bridge_tag`) and the placements' resolution of the key,

then add the per-blob rows. Every step is a `db_engine.spec` read, so the functions run on any backend; `reader` is
anything with `select_rows()` (a `BaseDBEngine`, a read session).
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from db_engine.spec import Join, NotExists, Select, Value, add, gt, in_select

# m_symbol_own.kind of the rows a blob stores itself instead of sharing them through its tags
KIND_REFS = 1
KIND_DEFS = 2
# Fan-out probe threshold before falling back to checkpoint-driven (vid, ast_id) resolution
HIGH_DEGREE_PROBE_LIMIT = 4000


def _placements(own_kind: int) -> tuple[list[Join], Any]:
    """Joins placing a tag (`ti`) in blobs (`bt`) and resolving its key to the target (`bs`), plus the "not stored
    per blob" condition."""
    joins = [
        Join(("m_bridge_tag", "bt"), {"bt.tag_id": "ti.tag_id"}),
        Join(("m_blob_symbol", "bs"), {"bs.blob_id": "bt.blob_id", "bs.sym_key": "ti.sym_key"}),
    ]
    not_own = NotExists(
        ("m_symbol_own", "o"), {"o.blob_id": "bt.blob_id", "o.tag_id": "bt.tag_id"}, {"o.kind": own_kind}
    )
    return joins, not_own


def _target_keys(targets: Sequence[int]) -> Any:
    """`ti.sym_key IN (the symbol keys of the targets)` as one subquery: the database joins from the keys instead of
    receiving them as a chunked list (one statement, not one per 1000 keys of a common function)."""
    return in_select("ti.sym_key", Select(("m_blob_symbol", "k"), ["k.sym_key"], {"k.target_ast_id": list(targets)}, distinct=True))


def _checkpoint_scoped_refs(
    reader: Any,
    targets: list[int],
    *,
    commit_id: int,
    role: int | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Resolve shared symbol references driving from the revision's checkpoint (`(commit_id, ast_id) -> m_blob_symbol`).

    Used when a high-degree symbol (`printk`, `kfree`) exceeds `HIGH_DEGREE_PROBE_LIMIT` across all versions, so
    `LIMIT` is pushed down against the target revision's blobs rather than scanning millions of historical rows.
    """
    if not hasattr(reader, "existing_tables") or "m_bridge_checkpoint" not in reader.existing_tables(["m_bridge_checkpoint"]):
        return []
    where: dict[str, Any] = {
        "bc.commit_id": int(commit_id),
        "bc.blob_id": gt(0),
        "a.ast_id": targets,
        "o.blob_id": None,
    }
    if role is not None:
        where["ti.role"] = int(role)
    joins = [
        Join(("m_ast", "a"), {}),
        Join(("m_blob_symbol", "bs"), {"bs.target_ast_id": "a.ast_id", "bs.blob_id": "bc.blob_id"}),
        Join(("m_tag_ref", "ti"), {"ti.sym_key": "bs.sym_key"}),
        Join(("m_bridge_tag", "bt"), {"bt.blob_id": "bs.blob_id", "bt.tag_id": "ti.tag_id"}),
        Join(
            ("m_symbol_own", "o"),
            {"o.blob_id": "bt.blob_id", "o.tag_id": "bt.tag_id", "o.kind": Value(KIND_REFS)},
            "left",
        ),
    ]
    return list(reader.select_rows(
        ("m_bridge_checkpoint", "bc"),
        {
            "role": "ti.role",
            "line": add("bt.line_s", "ti.rel_line"),
            "char_s": "ti.char_s",
            "fid": "bt.blob_id",
            "tag_id": "ti.tag_id",
            "ast_id": "bs.target_ast_id",
        },
        where,
        joins=joins,
        limit=limit,
        as_dict=True,
        hint="straight_join",
    ))


def symbol_refs_of_targets(
    reader: Any,
    ast_ids: Sequence[int],
    *,
    role: int | None = None,
    with_caller: bool = False,
    limit: int | None = None,
    commit_id: int | None = None,
) -> list[dict[str, Any]]:
    """References of `ast_ids`: dicts `role, line, char_s, fid (blob), tag_id, ast_id` (+ `caller`, the AST name of the
    tag the reference sits in). Per-blob rows come first, then the shared ones; `limit` caps the result."""
    targets = list(dict.fromkeys(int(a) for a in ast_ids))
    if not targets:
        return []
    per_blob_where: dict[str, Any] = {"r.ast_id": targets}
    if role is not None:
        per_blob_where["r.role"] = int(role)
    rows: list[dict[str, Any]] = list(reader.select_rows(
        ("m_blob_symbol_ref", "r"),
        {"role": "r.role", "line": "r.line", "char_s": "r.char_s", "fid": "r.blob_id", "tag_id": "r.tag_id", "ast_id": "r.ast_id"},
        per_blob_where,
        limit=limit,
        as_dict=True,
    ))
    if limit is None or len(rows) < limit or commit_id is not None:
        rem = None if limit is None else (limit if commit_id is not None and len(rows) >= limit else limit - len(rows))
        probe_cap = max(rem * 4, HIGH_DEGREE_PROBE_LIMIT) if (rem is not None and commit_id is not None) else rem
        shared_joins = [
            Join(("m_tag_ref", "ti"), {"ti.sym_key": "bs.sym_key"}),
            Join(("m_bridge_tag", "bt"), {"bt.blob_id": "bs.blob_id", "bt.tag_id": "ti.tag_id"}),
            Join(
                ("m_symbol_own", "o"),
                {"o.blob_id": "bt.blob_id", "o.tag_id": "bt.tag_id", "o.kind": Value(KIND_REFS)},
                "left",
            ),
        ]
        shared_where: dict[str, Any] = {"bs.target_ast_id": targets, "o.blob_id": None}
        if role is not None:
            shared_where["ti.role"] = int(role)
        shared_rows = list(reader.select_rows(
            ("m_blob_symbol", "bs"),
            {
                "role": "ti.role",
                "line": add("bt.line_s", "ti.rel_line"),
                "char_s": "ti.char_s",
                "fid": "bt.blob_id",
                "tag_id": "ti.tag_id",
                "ast_id": "bs.target_ast_id",
            },
            shared_where,
            joins=shared_joins,
            order_by=() if probe_cap is not None else ("bt.blob_id", "ti.tag_id", "ti.ref_idx"),
            limit=probe_cap,
            as_dict=True,
            hint="straight_join",
        ))
        used_scoped = False
        if (
            commit_id is not None
            and rem is not None
            and probe_cap is not None
            and len(shared_rows) >= probe_cap
        ):
            scoped = _checkpoint_scoped_refs(
                reader, targets, commit_id=int(commit_id), role=role, limit=rem
            )
            if scoped:
                shared_rows = scoped
                used_scoped = True
        if rem is not None and len(shared_rows) > rem:
            shared_rows = shared_rows[:rem]
        shared_rows.sort(key=lambda r: (int(r.get("fid") or 0), int(r.get("tag_id") or 0), int(r.get("line") or 0), int(r.get("char_s") or 0)))
        if used_scoped:
            rows = shared_rows
        else:
            rows.extend(shared_rows)
    if limit is not None:
        rows = rows[:limit]
    if with_caller:
        tag_ids = sorted({int(r["tag_id"]) for r in rows if r["tag_id"] is not None})
        callers: dict[int, Any] = {}
        if tag_ids:
            for tag_id, name in reader.select_rows(
                ("m_tag", "t"), ["t.tag_id", "a.name"], {"t.tag_id": tag_ids},
                joins=[Join(("m_ast", "a"), {"a.ast_id": "t.ast_id"}, "left")], as_dict=False,
            ):
                callers[int(tag_id)] = name
        for r in rows:
            r["caller"] = callers.get(int(r["tag_id"])) if r["tag_id"] is not None else None
    return rows


def symbol_def_blobs_of_targets(reader: Any, ast_ids: Sequence[int]) -> set[tuple[int, int]]:
    """Distinct `(ast_id, blob_id)` definitions of `ast_ids` (per-blob rows and the blobs placing a shared definition)."""
    targets = list(dict.fromkeys(int(a) for a in ast_ids))
    if not targets:
        return set()
    out: set[tuple[int, int]] = {
        (int(a), int(b))
        for a, b in reader.select_rows(
            ("m_blob_symbol_def", "d"), ["d.ast_id", "d.blob_id"], {"d.ast_id": targets}, distinct=True, as_dict=False
        )
    }
    joins = [
        Join(("m_tag_def", "ti"), {"ti.name": "a.name"}),
        Join(("m_bridge_tag", "bt"), {"bt.tag_id": "ti.tag_id"}),
        Join(
            ("m_blob_symbol", "bs"),
            {"bs.target_ast_id": "a.ast_id", "bs.blob_id": "bt.blob_id", "bs.sym_key": "ti.sym_key"},
        ),
        Join(
            ("m_symbol_own", "o"),
            {"o.blob_id": "bt.blob_id", "o.tag_id": "bt.tag_id", "o.kind": Value(KIND_DEFS)},
            "left",
        ),
    ]
    out.update(
        (int(a), int(b))
        for a, b in reader.select_rows(
            ("m_ast", "a"),
            ["bs.target_ast_id", "bt.blob_id"],
            {"a.ast_id": targets, "o.blob_id": None},
            joins=joins,
            distinct=True,
            as_dict=False,
            hint="straight_join",
        )
    )
    return out
