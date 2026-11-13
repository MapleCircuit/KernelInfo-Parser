"""parser/maintainer_ast/maintainer_store.py - Read the stored MAINTAINERS / CREDITS model (read-only).

Shared by the parser (Step 6.7 and `--backfill-maintainer-files`) and the webapp
(`webapp/backend/services/maintainer_service.py`), so both resolve a blob's sections the same way
(parser/maintainer_ast/MAINTAINER_API.md section 2).

Sections and CREDITS entries are content addressed: `MaintainerManager` stores a section row only when
the section is new or changed, so an unchanged section keeps the row stored with an earlier MAINTAINERS
blob. A blob's rows are therefore found through its tags (`m_bridge_tag -> m_tag -> ast_id`). The AST of
a section is keyed by its name only, so one `ast_id` names every stored version of the section; the
version a blob holds is chosen per name:

1. a row whose own blob places the same tag (identical text), the newest such row;
2. else the newest row stored with a blob no newer than this one (the section changed only in fields
   the parser does not compare);
3. else the newest row.

The cursors may return tuples (parser) or dicts (webapp).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable, Sequence

if TYPE_CHECKING:
    from db_engine.base import BaseDBEngine

from db_engine.spec import Join, gt
from parser.maintainer_ast.maintainer_matcher import MaintainerMatcher
from parser.maintainer_ast.maintainer_types import MaintainerSection


def _col(row: Any, idx: int, key: str) -> Any:
    return row[key] if isinstance(row, dict) else row[idx]


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return "" if value is None else str(value)


def pick_versions(rows: Iterable[tuple[Any, int, int, bool]], blob_id: int) -> dict[Any, int]:
    """Choose one row id per key from (key, row_id, row_blob_id, exact) candidates (rules in the module doc)."""
    best: dict[Any, tuple[int, int]] = {}
    for key, row_id, row_blob, exact in rows:
        rank = (2 if exact else 0) + (1 if int(row_blob) <= int(blob_id) else 0)
        cand = (rank, int(row_id))
        if cand > best.get(key, (-1, -1)):
            best[key] = cand
    return {key: row_id for key, (_rank, row_id) in best.items()}


def section_key(name: Any) -> str:
    """Lower-cased, stripped section name: the key sections are matched by across MAINTAINERS blobs."""
    return _text(name).strip().lower()


def blob_section_ids(db: BaseDBEngine, blob_id: int) -> dict[str, int]:
    """Section key (see `section_key`) -> sec_id of the section versions one MAINTAINERS blob holds."""
    if not blob_id:
        return {}
    raw_rows = db.select_rows(
        ("m_bridge_tag", "bt"),
        ["s.sec_id", "s.name", "s.blob_id", "bt2.blob_id"],
        {"bt.blob_id": int(blob_id), "s.sec_id": gt(0)},
        joins=[
            Join(("m_tag", "t"), {"t.tag_id": "bt.tag_id"}),
            Join(("m_maintainer_section", "s"), {"s.ast_id": "t.ast_id"}),
            Join(("m_bridge_tag", "bt2"), {"bt2.blob_id": "s.blob_id", "bt2.tag_id": "bt.tag_id"}, "left"),
        ],
        as_dict=False,
    )
    rows = (
        (section_key(_col(r, 1, "name")), int(_col(r, 0, "sec_id")), int(_col(r, 2, "blob_id")), _col(r, 3, "exact_tag") is not None)
        for r in raw_rows
    )
    return pick_versions(rows, blob_id)


def blob_credit_ids(db: BaseDBEngine, blob_id: int) -> list[int]:
    """credit_ids of the entry versions one CREDITS blob holds (one per entry name and person), sorted."""
    if not blob_id:
        return []
    raw_rows = db.select_rows(
        ("m_bridge_tag", "bt"),
        ["ce.credit_id", "ce.ast_id", "ce.person_id", "ce.blob_id", "bt2.blob_id"],
        {"bt.blob_id": int(blob_id), "ce.credit_id": gt(0)},
        joins=[
            Join(("m_tag", "t"), {"t.tag_id": "bt.tag_id"}),
            Join(("m_credits_entry", "ce"), {"ce.ast_id": "t.ast_id"}),
            Join(("m_bridge_tag", "bt2"), {"bt2.blob_id": "ce.blob_id", "bt2.tag_id": "bt.tag_id"}, "left"),
        ],
        as_dict=False,
    )
    rows = (
        (
            (int(_col(r, 1, "ast_id")), int(_col(r, 2, "person_id"))),
            int(_col(r, 0, "credit_id")),
            int(_col(r, 3, "blob_id")),
            _col(r, 4, "exact_tag") is not None,
        )
        for r in raw_rows
    )
    return sorted(pick_versions(rows, blob_id).values())


def version_blob(db: BaseDBEngine, commit_id: int, fname: str, tree: str = "m_checkpoint_tree") -> int | None:
    """blob_id of `fname` (MAINTAINERS, CREDITS) in the checkpoint of commit_id, or None."""
    if not commit_id:
        return None
    row = db.select_one(
        (tree, "t"),
        ["t.blob_id"],
        {"t.commit_id": int(commit_id), "fn.fname": fname},
        joins=[Join(("m_file_name", "fn"), {"fn.fnid": "t.fnid"})],
        as_dict=False,
    )
    return int(_col(row, 0, "blob_id")) if row else None


def sections_with_rows(db: BaseDBEngine, sec_ids: Iterable[int]) -> set[int]:
    """The sec_ids among sec_ids that have at least one m_maintainer_file row."""
    ids = sorted({int(s) for s in sec_ids})
    found: set[int] = set()
    for i in range(0, len(ids), 1000):
        chunk = ids[i : i + 1000]
        raw_rows = db.select_rows("m_maintainer_file", ["sec_id"], {"sec_id": chunk}, distinct=True, as_dict=False)
        found.update(int(_col(r, 0, "sec_id")) for r in raw_rows)
    return found


def map_files(
    sections: Sequence[MaintainerSection],
    sec_ids: dict[str, int],
    changed: set[int] | None,
    all_paths: Iterable[str],
    new_paths: Iterable[str],
) -> set[tuple[int, str]]:
    """(sec_id, path) pairs Step 6.7 stores for one version.

    sections: the version's parsed MAINTAINERS sections; sec_ids: section key -> stored sec_id
    (`blob_section_ids`); changed: sec_ids whose file rows are not known yet (None = all of them). A
    section matches a path through its own patterns only, so a pair is a fact of the section version:
    changed sections are matched against every path, the others only against the paths new in this
    version (their other pairs were stored with an earlier version)."""
    known = [(sec, sec_ids[section_key(sec.name)]) for sec in sections if section_key(sec.name) in sec_ids]
    pairs: set[tuple[int, str]] = set()

    def _match(matcher: MaintainerMatcher, paths: Iterable[str]) -> None:
        for path in paths:
            for sec in matcher.match_file(path):
                sid = sec_ids.get(section_key(sec.name))
                if sid is not None:
                    pairs.add((sid, path))

    changed_secs = [sec for sec, sid in known if changed is None or sid in changed]
    if changed_secs:
        _match(MaintainerMatcher(changed_secs), all_paths)
    new_paths = list(new_paths)
    if new_paths and len(changed_secs) < len(known):
        _match(MaintainerMatcher([sec for sec, _sid in known]), new_paths)
    return pairs
