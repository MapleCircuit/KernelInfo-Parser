"""db_engine/read_intents.py - Webapp intent reads implemented once over query specs.

`ReadIntents` is a mixin for every read session (`ReadSession` of MariaDB, `MockReadSession`, `SQLiteReadSession`):
it needs only `select_rows()` / `aggregate()` / `column_exists()` / `existing_tables()` / `resolve_relation_name()`
on `self`. A backend session may override a method with a tuned form (the MariaDB `ReadSession` keeps derived-table
SQL for a few); the others inherit these.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from db_engine.spec import (
    Agg,
    AnyOf,
    Case,
    Exists,
    Join,
    Length,
    Order,
    Pred,
    ge,
    gt,
    ieq,
    le,
    like,
    lt,
    ne,
)


def _text(value: Any) -> Any:
    """Name column value as str (a BINARY / BLOB column can come back as bytes)."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    return value


class ReadIntents:
    """Named read operations of the webapp, built from spec reads (all rows are dicts)."""

    # ---- checkpoint tree -----------------------------------------------------------------------------------------
    def checkpoint_tree_source(self) -> str:
        """Relation holding every version's full checkpoint tree: `m_checkpoint_tree`, or `m_bridge_checkpoint` on a
        database created before that view existed. Resolved once per engine (re-checked when absent) and usable as a
        `Select` / `Join` source (`(self.checkpoint_tree_source(), "bc")`)."""
        return self.resolve_relation_name("m_checkpoint_tree")  # type: ignore[attr-defined, no-any-return]

    def checkpoint_tree_exists(self, commit_id: int) -> bool:
        """True when the version `commit_id` has a stored checkpoint tree."""
        return bool(self.row_exists(self.checkpoint_tree_source(), {"commit_id": int(commit_id)}))  # type: ignore[attr-defined]

    def checkpoint_tree_files(
        self,
        commit_id: int,
        *,
        fnames: Sequence[str] | None = None,
        name: Any = None,
        files_only: bool = False,
        with_ftype: bool = False,
        include_unnamed: bool = False,
        order_by_name: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """`{fnid, fname, blob_id[, ftype]}` of the files of a version's checkpoint tree.

        `fnames` keeps the exact names listed, `name` is a `Cond` on the file name (`like(prefix, "prefix")`),
        `files_only` drops directories (`m_file_blob.ftype = 0`), `with_ftype` adds `ftype`, `include_unnamed` keeps
        entries without an `m_file_name` row (`fname` is None for them), `order_by_name` sorts by name.
        """
        if fnames is not None and not fnames:
            return []
        joins: list[Join] = [Join(("m_file_name", "fn"), {"fn.fnid": "bc.fnid"}, "left" if include_unnamed else "inner")]
        cols: dict[str, Any] = {"fnid": "bc.fnid", "fname": "fn.fname", "blob_id": "bc.blob_id"}
        where: list[Any] = [{"bc.commit_id": int(commit_id)}]
        if files_only or with_ftype:
            joins.append(Join(("m_file_blob", "fb"), {"fb.blob_id": "bc.blob_id"}))
            if with_ftype:
                cols["ftype"] = "fb.ftype"
            if files_only:
                where.append({"fb.ftype": ne(0)})
        if fnames is not None:
            where.append({"fn.fname": list(fnames)})
        if name is not None:
            where.append({"fn.fname": name})
        return self.select_rows(  # type: ignore[attr-defined, no-any-return]
            (self.checkpoint_tree_source(), "bc"),
            cols,
            where,
            joins=joins,
            order_by=["fn.fname"] if order_by_name else (),
            limit=limit,
            as_dict=True,
        )

    def checkpoint_find_file(
        self,
        commit_id: int,
        candidates: Sequence[str],
        suffix: str | None = None,
    ) -> str | None:
        """Best matching file path of a checkpoint tree: an exact `candidates` name (the earlier the better) or, when
        `suffix` is given, a path ending with it; the shortest path among equals. Directories are skipped."""
        if not candidates:
            return None
        names: list[Any] = [{"fn.fname": list(candidates)}]
        if suffix is not None:
            names.append({"fn.fname": like(suffix, "suffix")})
        rows = self.select_rows(  # type: ignore[attr-defined]
            ("m_file_name", "fn"),
            ["fn.fname"],
            [{"bc.commit_id": int(commit_id)}, {"fb.ftype": ne(0)}, AnyOf(*names)],
            joins=[
                Join((self.checkpoint_tree_source(), "bc"), {"bc.fnid": "fn.fnid"}),
                Join(("m_file_blob", "fb"), {"fb.blob_id": "bc.blob_id"}),
            ],
            order_by=[
                Case([({"fn.fname": c}, i) for i, c in enumerate(candidates)], len(candidates)),
                Length("fn.fname"),
            ],
            limit=1,
            as_dict=False,
        )
        if rows and rows[0] and rows[0][0] is not None:
            return str(_text(rows[0][0]))
        return None

    # ---- commits -------------------------------------------------------------------------------------------------
    def list_network_commits(
        self,
        network_id: int,
        *,
        hash_prefix: str | None = None,  # noqa: A002 - public keyword
        text: str | None = None,
        authored_before: int | None = None,
        id_min: int | None = None,
        id_min_exclusive: bool = False,
        id_max: int | None = None,
        newest_first: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Commits of a git network (`commit_id, commit_hash, author_date, subject, author_id, author_name,
        author_email, files_count`) matching a hash prefix, subject / author text, date and id bounds."""
        from db_engine.spec import hash_prefix as hash_prefix_cond

        where: list[Any] = [{"c.commit_id": gt(0), "c.network_id": int(network_id)}]
        if hash_prefix:
            where.append({"c.commit_hash": hash_prefix_cond(hash_prefix)})
        if text:
            where.append(AnyOf({"c.subject": like(text, ci=True)}, {"p.name": like(text, ci=True)}))
        if authored_before is not None:
            where.append({"c.author_date": lt(authored_before)})
        if id_min is not None:
            where.append({"c.commit_id": gt(id_min) if id_min_exclusive else ge(id_min)})
        if id_max is not None:
            where.append({"c.commit_id": le(id_max)})
        rows = self.select_rows(  # type: ignore[attr-defined]
            ("m_commit", "c"),
            {
                "commit_id": "c.commit_id",
                "commit_hash": "c.commit_hash",
                "author_date": "c.author_date",
                "subject": "c.subject",
                "author_id": "p.person_id",
                "author_name": "p.name",
                "author_email": "p.email",
            },
            where,
            joins=[Join(("m_maintainer_person", "p"), {"p.person_id": "c.author_id"})],
            order_by=["-c.commit_id" if newest_first else "c.commit_id"],
            limit=int(limit),
            offset=int(offset),
            as_dict=True,
        )
        counts: dict[int, int] = {}
        if rows:
            for r in self.aggregate(  # type: ignore[attr-defined]
                "m_commit_file",
                {"n": Agg("COUNT", "*")},
                {"commit_id": [r["commit_id"] for r in rows]},
                group_by=["commit_id"],
                as_dict=True,
            ):
                counts[int(r["commit_id"])] = int(r["n"])
        for r in rows:
            if isinstance(r.get("commit_hash"), (bytearray, memoryview)):
                r["commit_hash"] = bytes(r["commit_hash"])
            r["files_count"] = counts.get(int(r["commit_id"]), 0)
        return rows

    # ---- maintainers ---------------------------------------------------------------------------------------------
    _SECTION_COLUMNS = ("sec_id", "name", "status", "scm_tree", "web_page", "mailing_list", "ast_id")
    _SECTION_EXTENDED = ("patchwork", "bug_url", "chat_channel", "profile_doc")

    def maintainer_sections(
        self,
        sec_ids: Sequence[int] | None = None,
        *,
        sec_id: int | None = None,
        name: str | None = None,
        member_person_ids: Sequence[int] | None = None,
        text: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Maintainer sections (newest first) with status, URLs and, on a database with them, the extended columns
        (`patchwork`, `bug_url`, `chat_channel`, `profile_doc`; None otherwise)."""
        has_ext = self.column_exists("m_maintainer_section", "patchwork")  # type: ignore[attr-defined]
        names = self._SECTION_COLUMNS + (self._SECTION_EXTENDED if has_ext else ())
        where: list[Any] = []
        if sec_ids is not None:
            if not sec_ids:
                return []
            where.append({"s.sec_id": list(sec_ids)})
        if sec_id is not None:
            where.append({"s.sec_id": sec_id})
        if name is not None:
            where.append({"s.name": name})
        if member_person_ids is not None:
            if not member_person_ids:
                return []
            where.append(
                Exists(("m_maintainer_member", "mem"), {"mem.sec_id": "s.sec_id"}, {"mem.person_id": list(member_person_ids)})
            )
        if text is not None:
            where.append(
                AnyOf(
                    {"s.name": like(text, ci=True)},
                    [
                        Exists(
                            ("m_maintainer_member", "mem"),
                            {"mem.sec_id": "s.sec_id"},
                            AnyOf({"per.name": like(text, ci=True)}, {"per.email": like(text, ci=True)}),
                            joins=[Join(("m_maintainer_person", "per"), {"per.person_id": "mem.person_id"})],
                        )
                    ],
                )
            )
        rows = self.select_rows(  # type: ignore[attr-defined]
            ("m_maintainer_section", "s"),
            {n: f"s.{n}" for n in names},
            where or None,
            order_by=["-s.sec_id"],
            limit=limit,
            as_dict=True,
        )
        for r in rows:
            for k in self._SECTION_COLUMNS + self._SECTION_EXTENDED:
                r.setdefault(k, None)
        return rows

    def maintainer_developer_directory(
        self,
        sec_ids: Sequence[int] | None = None,
        credit_ids: Sequence[int] | None = None,
    ) -> list[dict]:
        """One profile per registered person: `person_id, name, email, subsystems_count, is_maintainer,
        is_reviewer, in_credits, has_commits`. `sec_ids` / `credit_ids` restrict the memberships and CREDITS entries
        counted (an empty list counts none)."""
        persons = self.select_rows(  # type: ignore[attr-defined]
            "m_maintainer_person", ["person_id", "name", "email"], {"person_id": gt(0)}, as_dict=True
        )
        by_person: dict[int, dict[str, Any]] = {}
        if sec_ids is None or sec_ids:
            where: list[Any] = []
            if sec_ids is not None:
                where.append({"s.sec_id": list(sec_ids)})
            for r in self.select_rows(  # type: ignore[attr-defined]
                ("m_maintainer_member", "m"),
                {"person_id": "m.person_id", "role_type": "m.role_type", "sname": "s.name"},
                where or None,
                joins=[Join(("m_maintainer_section", "s"), {"s.sec_id": "m.sec_id"})],
                as_dict=True,
            ):
                acc = by_person.setdefault(int(r["person_id"]), {"names": set(), "maint": 0, "rev": 0})
                acc["names"].add(r["sname"])
                if r["role_type"] == 1:
                    acc["maint"] = 1
                elif r["role_type"] == 2:
                    acc["rev"] = 1
        in_credits: set[int] = set()
        if credit_ids is None or credit_ids:
            credit_where: Any = {"credit_id": list(credit_ids)} if credit_ids is not None else None
            in_credits = {
                int(v) for v in self.select_values("m_credits_entry", "person_id", credit_where, distinct=True)  # type: ignore[attr-defined]
            }
        with_commits = {
            int(v)
            for v in self.select_values(  # type: ignore[attr-defined]
                ("m_maintainer_person", "p"),
                "p.person_id",
                [
                    {"p.person_id": gt(0)},
                    Exists(("m_commit", "com"), {"com.author_id": "p.person_id"}),
                ],
            )
        }
        out: list[dict] = []
        for p in persons:
            pid = int(p["person_id"])
            acc = by_person.get(pid)
            out.append(
                {
                    "person_id": pid,
                    "name": p["name"],
                    "email": p["email"],
                    "subsystems_count": len(acc["names"]) if acc else 0,
                    "is_maintainer": acc["maint"] if acc else 0,
                    "is_reviewer": acc["rev"] if acc else 0,
                    "in_credits": 1 if pid in in_credits else 0,
                    "has_commits": 1 if pid in with_commits else 0,
                }
            )
        return out

    def find_maintainer_person(
        self,
        raw: str,
        stripped: str,
        email: str,
        name: str,
    ) -> dict | None:
        """Person (`person_id, name, email`) of an id, an e-mail address or a name (case-insensitive; a named person
        wins over an unnamed one, then the lowest id)."""
        strip = "<>'\" \t\r\n"
        clean_raw, clean_stripped = raw.strip(strip), stripped.strip(strip)
        clean_email, clean_name = email.strip(strip), name.strip(strip)
        if clean_stripped.isdigit():
            return self.select_one(  # type: ignore[attr-defined, no-any-return]
                "m_maintainer_person", ["person_id", "name", "email"], {"person_id": int(clean_stripped)}, as_dict=True
            )
        branches: list[Any] = [{"email": ieq(clean_raw)}, {"name": ieq(clean_raw)}, {"email": ieq(clean_stripped)}]
        if clean_email:
            branches.append({"email": ieq(clean_email)})
        if clean_name:
            branches.append({"name": ieq(clean_name)})
        return self.select_one(  # type: ignore[attr-defined, no-any-return]
            "m_maintainer_person",
            ["person_id", "name", "email"],
            AnyOf(*branches),
            order_by=[Order(Case([({"name": ""}, 1)], 0)), "person_id"],
            as_dict=True,
        )

    # ---- symbols -------------------------------------------------------------------------------------------------
    def symbol_name_page(
        self,
        prefix: str,
        *,
        after: tuple[int, str] | None = None,
        limit: int = 400,
    ) -> list[str]:
        """Distinct symbol names starting with `prefix`, shortest first then alphabetical, keyset paged after
        `(len(name), name)`."""
        where: list[Any] = [{"name": like(prefix, "prefix")}]
        if after is not None:
            where.append(
                AnyOf([Pred(Length("name"), gt(after[0]))], [Pred(Length("name"), after[0]), {"name": gt(after[1])}])
            )
        names: set[str] = set()
        for tbl in ("m_tag_def", "m_blob_symbol_def"):
            if not self.existing_tables([tbl]):  # type: ignore[attr-defined]
                continue
            for val in self.select_values(  # type: ignore[attr-defined]
                tbl, "name", where, distinct=True, order_by=[Length("name"), "name"], limit=int(limit)
            ):
                if val is not None:
                    names.add(str(_text(val)))
        return sorted(names, key=lambda n: (len(n), n))[:limit]

    def symbol_refs_of_targets(
        self,
        ast_ids: Sequence[int],
        *,
        role: int | None = None,
        with_caller: bool = False,
        limit: int | None = None,
    ) -> list[dict]:
        """References of target ASTs (`role, line, char_s, fid, tag_id, ast_id` [+ `caller`]); see `symbol_rows`."""
        from db_engine import symbol_rows

        return symbol_rows.symbol_refs_of_targets(self, ast_ids, role=role, with_caller=with_caller, limit=limit)

    def symbol_def_blobs_of_targets(self, ast_ids: Sequence[int]) -> set[tuple[int, int]]:
        """Distinct `(ast_id, blob_id)` definitions of target ASTs; see `symbol_rows`."""
        from db_engine import symbol_rows

        return symbol_rows.symbol_def_blobs_of_targets(self, ast_ids)

