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


def _delta_where(
    series_track: str | None = None,
    *,
    from_ord: int | None = None,
    to_ord: int | None = None,
    base_mainline_tag: str | None = None,
    tags: Sequence[str] | None = None,
    subsystem_name: str | None = None,
    exclude_sentinel: bool = False,
) -> list[Any]:
    """Build spec Where terms for `(series_track, from_ord, to_ord]` insights delta queries."""
    where: list[Any] = []
    if series_track is not None:
        where.append({"series_track": series_track})
    if from_ord is not None:
        where.append({"series_ord": gt(int(from_ord))})
    if to_ord is not None:
        where.append({"series_ord": le(int(to_ord))})
    if base_mainline_tag is not None:
        where.append({"base_mainline_tag": base_mainline_tag})
    if tags is not None:
        where.append({"version_tag": list(tags)})
    if subsystem_name is not None:
        where.append({"subsystem_name": subsystem_name})
    elif exclude_sentinel:
        where.append({"subsystem_name": ne("")})
    return where


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

    # ---- kapi ----------------------------------------------------------------------------------------------------
    def kapi_resolve_commit_id(self, version_or_sha: str, repo_id: int | None = None) -> int | None:
        """Resolve a tag/ref name or commit hash (prefix or full 40-char SHA) to `commit_id`."""
        from db_engine.spec import hash_prefix as hash_prefix_cond

        key = (version_or_sha or "").strip()
        if not key:
            return None
        ref_where: dict[str, Any] = {"ref_name": key}
        if repo_id is not None:
            ref_where["repo_id"] = int(repo_id)
        ref_row = self.select_one(  # type: ignore[attr-defined]
            "m_git_ref", ["commit_id"], ref_where, order_by=["-commit_id"], as_dict=True
        )
        if ref_row and ref_row.get("commit_id") is not None:
            return int(ref_row["commit_id"])
        if 4 <= len(key) <= 40 and all(c in "0123456789abcdefABCDEF" for c in key):
            where: list[Any] = [{"c.commit_hash": hash_prefix_cond(key)}]
            joins: list[Join] = []
            if repo_id is not None:
                joins.append(Join(("m_repository", "rep"), {"rep.network_id": "c.network_id"}))
                where.append({"rep.repo_id": int(repo_id)})
            commit_row = self.select_one(  # type: ignore[attr-defined]
                ("m_commit", "c"),
                ["c.commit_id"],
                where,
                joins=joins,
                order_by=["-c.commit_id"],
                as_dict=True,
            )
            if commit_row and commit_row.get("commit_id") is not None:
                return int(commit_row["commit_id"])
        return None

    def kapi_catalog(self, commit_id: int, kind: str, arch: str = "") -> dict[str, Any] | None:
        """Return the materialized `m_kapi_catalog` row (including `data` BLOB) for `(commit_id, arch, kind)`."""
        if not self.existing_tables(["m_kapi_catalog"]):  # type: ignore[attr-defined]
            return None
        row = self.select_one(  # type: ignore[attr-defined]
            "m_kapi_catalog",
            [
                "commit_id",
                "arch",
                "kind",
                "catalog_format",
                "kapi_format",
                "total_count",
                "summary_json",
                "data",
                "built_at",
            ],
            {"commit_id": int(commit_id), "arch": arch, "kind": kind},
            as_dict=True,
        )
        if row is None:
            return None
        row["arch"] = _text(row.get("arch", ""))
        row["kind"] = _text(row.get("kind", ""))
        if row.get("summary_json") is not None:
            row["summary_json"] = _text(row["summary_json"])
        if isinstance(row.get("data"), (bytearray, memoryview)):
            row["data"] = bytes(row["data"])
        return row

    def kapi_catalog_meta(self, commit_id: int, arch: str | None = "") -> list[dict[str, Any]]:
        """Return metadata rows (without `data` BLOB) from `m_kapi_catalog` for `commit_id`."""
        if not self.existing_tables(["m_kapi_catalog"]):  # type: ignore[attr-defined]
            return []
        where: dict[str, Any] = {"commit_id": int(commit_id)}
        if arch is not None:
            where["arch"] = arch
        rows = self.select_rows(  # type: ignore[attr-defined]
            "m_kapi_catalog",
            ["commit_id", "arch", "kind", "catalog_format", "kapi_format", "total_count", "summary_json", "built_at"],
            where,
            order_by=["arch", "kind"],
            as_dict=True,
        )
        for r in rows:
            r["arch"] = _text(r.get("arch", ""))
            r["kind"] = _text(r.get("kind", ""))
            if r.get("summary_json") is not None:
                r["summary_json"] = _text(r["summary_json"])
        return rows

    def kapi_release_summary(self, commit_id: int, arch: str = "x86_64") -> dict[str, Any] | None:
        """Return the materialized `m_kapi_release_summary` row for `(commit_id, arch)`."""
        if not self.existing_tables(["m_kapi_release_summary"]):  # type: ignore[attr-defined]
            return None
        row = self.select_one(  # type: ignore[attr-defined]
            "m_kapi_release_summary",
            [
                "commit_id",
                "arch",
                "catalog_format",
                "kapi_format",
                "syscalls_count",
                "exports_count",
                "params_count",
                "tracepoints_count",
                "modules_count",
                "devices_count",
                "subsystems_json",
                "built_at",
            ],
            {"commit_id": int(commit_id), "arch": arch},
            as_dict=True,
        )
        if row is None:
            return None
        row["arch"] = _text(row.get("arch", ""))
        if row.get("subsystems_json") is not None:
            row["subsystems_json"] = _text(row["subsystems_json"])
        return row

    # ---- insights ------------------------------------------------------------------------------------------------
    def release_segments(
        self,
        repo_id: int | None = None,
        *,
        chain: str | None = None,
        tags: Sequence[str] | None = None,
        segment_ids: Sequence[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Release segments from `m_release_segment` filtered by `repo_id`, `chain`, `tags`, or `segment_ids`."""
        if tags is not None and not tags:
            return []
        if segment_ids is not None and not segment_ids:
            return []
        where: dict[str, Any] = {}
        if repo_id is not None:
            where["repo_id"] = int(repo_id)
        if chain is not None:
            where["chain"] = chain
        if tags is not None:
            where["tag"] = list(tags)
        if segment_ids is not None:
            where["segment_id"] = [int(s) for s in segment_ids]
        rows = self.select_rows(  # type: ignore[attr-defined]
            "m_release_segment",
            [
                "segment_id",
                "repo_id",
                "tag",
                "tag_hash",
                "chain",
                "base_tag",
                "base_hash",
                "commits",
                "merges",
                "diff_base_tag",
                "files_changed",
                "lines_added",
                "lines_removed",
                "format",
            ],
            where or None,
            order_by=["segment_id"],
            as_dict=True,
        )
        for r in rows:
            for k in ("tag", "chain", "base_tag", "diff_base_tag"):
                if r.get(k) is not None:
                    r[k] = _text(r[k])
            for k in ("tag_hash", "base_hash"):
                if isinstance(r.get(k), (bytearray, memoryview)):
                    r[k] = bytes(r[k])
        return rows

    def release_pulls(self, segment_ids: Sequence[int] | int) -> list[dict[str, Any]]:
        """Pull-request records from `m_release_pull` for one or more `segment_ids`, ordered by `segment_id, pull_idx`."""
        ids = [int(segment_ids)] if isinstance(segment_ids, int) else [int(s) for s in segment_ids]
        if not ids or not self.existing_tables(["m_release_pull"]):  # type: ignore[attr-defined]
            return []
        rows = self.select_rows(  # type: ignore[attr-defined]
            "m_release_pull",
            [
                "segment_id",
                "pull_idx",
                "merge_hash",
                "p1_hash",
                "p2_hash",
                "merge_date",
                "commits",
                "subject",
                "source_url",
                "ref_name",
                "sender",
                "sec_id",
                "sec_name",
                "dirs_json",
                "body",
                "format",
            ],
            {"segment_id": ids},
            order_by=["segment_id", "pull_idx"],
            as_dict=True,
        )
        for r in rows:
            for k in ("subject", "source_url", "ref_name", "sender", "sec_name", "dirs_json", "body"):
                if r.get(k) is not None:
                    r[k] = _text(r[k])
            for k in ("merge_hash", "p1_hash", "p2_hash"):
                if isinstance(r.get(k), (bytearray, memoryview)):
                    r[k] = bytes(r[k])
        return rows

    def release_section_health(self, segment_ids: Sequence[int] | int) -> list[dict[str, Any]]:
        """Subsystem health rows from `m_release_section_health` for one or more `segment_ids`."""
        ids = [int(segment_ids)] if isinstance(segment_ids, int) else [int(s) for s in segment_ids]
        if not ids or not self.existing_tables(["m_release_section_health"]):  # type: ignore[attr-defined]
            return []
        rows = self.select_rows(  # type: ignore[attr-defined]
            "m_release_section_health",
            [
                "segment_id",
                "sec_name",
                "sec_id",
                "status",
                "commits",
                "reviewed_commits",
                "authors_json",
                "activity_json",
                "pat_hash",
                "format",
            ],
            {"segment_id": ids},
            order_by=["segment_id", "sec_name"],
            as_dict=True,
        )
        for r in rows:
            for k in ("sec_name", "status", "authors_json", "activity_json"):
                if r.get(k) is not None:
                    r[k] = _text(r[k])
            if isinstance(r.get("pat_hash"), (bytearray, memoryview)):
                r["pat_hash"] = bytes(r["pat_hash"])
        return rows

    def release_person_counts(self, segment_ids: Sequence[int] | int) -> list[dict[str, Any]]:
        """Aggregated `(person_id, role_type, commits)` across `segment_ids` from `m_release_person`."""
        ids = [int(segment_ids)] if isinstance(segment_ids, int) else [int(s) for s in segment_ids]
        if not ids:
            return []
        return self.aggregate(  # type: ignore[attr-defined, no-any-return]
            "m_release_person",
            {"person_id": "person_id", "role_type": "role_type", "commits": Agg("SUM", "commits")},
            {"segment_id": ids},
            group_by=["person_id", "role_type"],
            as_dict=True,
        )

    def person_first_releases(self, repo_id: int, person_ids: Sequence[int]) -> list[dict[str, Any]]:
        """First mainline release `segment_id` per `person_id` in `repo_id` from `m_person_first_release`."""
        pids = sorted({int(p) for p in person_ids})
        if not pids:
            return []
        out: list[dict[str, Any]] = []
        for i in range(0, len(pids), 1000):
            out.extend(
                self.select_rows(  # type: ignore[attr-defined]
                    "m_person_first_release",
                    ["person_id", "segment_id"],
                    {"repo_id": int(repo_id), "person_id": pids[i : i + 1000]},
                    as_dict=True,
                )
            )
        return out

    # ---- insights deltas -----------------------------------------------------------------------------------------
    def subsystem_version_deltas(
        self,
        series_track: str,
        *,
        from_ord: int | None = None,
        to_ord: int | None = None,
        tags: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Incremental subsystem delta rows from `insights_subsystem_version_delta` for `series_track` and `(from_ord, to_ord]`."""
        if tags is not None and not tags:
            return []
        if not self.existing_tables(["insights_subsystem_version_delta"]):  # type: ignore[attr-defined]
            return []
        where = _delta_where(series_track, from_ord=from_ord, to_ord=to_ord, tags=tags)
        rows = self.select_rows(  # type: ignore[attr-defined]
            "insights_subsystem_version_delta",
            [
                "version_tag",
                "subsystem_name",
                "parent_tag",
                "series_track",
                "series_ord",
                "release_ts",
                "maintainer_status",
                "commit_count",
                "merge_commit_count",
                "lines_added",
                "lines_removed",
                "files_touched_count",
                "distinct_author_count",
                "fixes_commit_count",
                "stable_cc_count",
                "reviewed_commit_count",
                "tested_commit_count",
            ],
            where or None,
            order_by=["series_ord", "version_tag", "subsystem_name"],
            as_dict=True,
        )
        for r in rows:
            for k in ("version_tag", "subsystem_name", "parent_tag", "series_track", "maintainer_status"):
                if r.get(k) is not None:
                    r[k] = _text(r[k])
        return rows

    def subsystem_author_deltas(
        self,
        series_track: str,
        *,
        from_ord: int | None = None,
        to_ord: int | None = None,
        tags: Sequence[str] | None = None,
        subsystem_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Incremental subsystem author delta rows from `insights_subsystem_author_delta` for `(from_ord, to_ord]`."""
        if tags is not None and not tags:
            return []
        if not self.existing_tables(["insights_subsystem_author_delta"]):  # type: ignore[attr-defined]
            return []
        where = _delta_where(
            series_track,
            from_ord=from_ord,
            to_ord=to_ord,
            tags=tags,
            subsystem_name=subsystem_name,
        )
        rows = self.select_rows(  # type: ignore[attr-defined]
            "insights_subsystem_author_delta",
            [
                "version_tag",
                "subsystem_name",
                "author_identity_id",
                "series_track",
                "series_ord",
                "author_key",
                "commit_count",
                "last_active_ts",
            ],
            where or None,
            order_by=["series_ord", "version_tag", "subsystem_name", "-commit_count", "author_identity_id"],
            as_dict=True,
        )
        for r in rows:
            for k in ("version_tag", "subsystem_name", "series_track", "author_key"):
                if r.get(k) is not None:
                    r[k] = _text(r[k])
        return rows

    def subsystem_distinct_authors(
        self,
        series_track: str,
        *,
        from_ord: int | None = None,
        to_ord: int | None = None,
        tags: Sequence[str] | None = None,
    ) -> dict[str, int]:
        """Exact `COUNT(DISTINCT author_identity_id)` per `subsystem_name` over `(from_ord, to_ord]`."""
        if tags is not None and not tags:
            return {}
        if not self.existing_tables(["insights_subsystem_author_delta"]):  # type: ignore[attr-defined]
            return {}
        where = _delta_where(series_track, from_ord=from_ord, to_ord=to_ord, tags=tags, exclude_sentinel=True)
        rows = self.aggregate(  # type: ignore[attr-defined]
            "insights_subsystem_author_delta",
            {"distinct_authors": Agg("COUNT", "author_identity_id", distinct=True)},
            where or None,
            group_by=["subsystem_name"],
            as_dict=True,
        )
        out: dict[str, int] = {}
        for r in rows:
            name = str(_text(r.get("subsystem_name", "")) or "")
            if name:
                out[name] = int(r.get("distinct_authors") or 0)
        return out

    def stable_version_deltas(
        self,
        series_track: str | None = None,
        *,
        from_ord: int | None = None,
        to_ord: int | None = None,
        base_mainline_tag: str | None = None,
        tags: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Incremental stable-release delta rows from `insights_stable_version_delta`."""
        if tags is not None and not tags:
            return []
        if not self.existing_tables(["insights_stable_version_delta"]):  # type: ignore[attr-defined]
            return []
        where = _delta_where(
            series_track,
            from_ord=from_ord,
            to_ord=to_ord,
            base_mainline_tag=base_mainline_tag,
            tags=tags,
        )
        rows = self.select_rows(  # type: ignore[attr-defined]
            "insights_stable_version_delta",
            [
                "version_tag",
                "subsystem_name",
                "parent_tag",
                "base_mainline_tag",
                "series_track",
                "series_ord",
                "release_ts",
                "total_commits",
                "backport_commits",
                "fixes_tag_commits",
                "cc_stable_commits",
                "cve_linked_commits",
                "lines_added",
                "lines_removed",
                "same_cycle_fixes_count",
                "cross_release_fixes_count",
                "sum_fix_latency_days",
                "resolved_fixes_count",
            ],
            where or None,
            order_by=["series_ord", "version_tag", "subsystem_name"],
            as_dict=True,
        )
        for r in rows:
            for k in ("version_tag", "subsystem_name", "parent_tag", "base_mainline_tag", "series_track"):
                if r.get(k) is not None:
                    r[k] = _text(r[k])
        return rows

    def stable_aggregated_metrics(
        self,
        series_track: str | None = None,
        *,
        from_ord: int | None = None,
        to_ord: int | None = None,
        base_mainline_tag: str | None = None,
        tags: Sequence[str] | None = None,
        by_subsystem: bool = False,
    ) -> list[dict[str, Any]]:
        """SQL `SUM(...)` aggregates over `insights_stable_version_delta` with `avg_fix_latency_days`."""
        if tags is not None and not tags:
            return []
        if not self.existing_tables(["insights_stable_version_delta"]):  # type: ignore[attr-defined]
            return []
        int_cols = (
            "total_commits",
            "backport_commits",
            "fixes_tag_commits",
            "cc_stable_commits",
            "cve_linked_commits",
            "lines_added",
            "lines_removed",
            "same_cycle_fixes_count",
            "cross_release_fixes_count",
            "resolved_fixes_count",
        )
        aggs = {c: Agg("SUM", c) for c in (*int_cols, "sum_fix_latency_days")}
        where = _delta_where(
            series_track,
            from_ord=from_ord,
            to_ord=to_ord,
            base_mainline_tag=base_mainline_tag,
            tags=tags,
            exclude_sentinel=by_subsystem,
        )
        rows = self.aggregate(  # type: ignore[attr-defined]
            "insights_stable_version_delta",
            aggs,
            where or None,
            group_by=["subsystem_name"] if by_subsystem else (),
            order_by=["subsystem_name"] if by_subsystem else (),
            as_dict=True,
        )
        if not rows:
            return []
        if not by_subsystem and rows[0].get("total_commits") is None:
            return []
        out: list[dict[str, Any]] = []
        for r in rows:
            item: dict[str, Any] = {}
            if by_subsystem:
                name = str(_text(r.get("subsystem_name", "")) or "")
                if not name:
                    continue
                item["subsystem_name"] = name
            for c in int_cols:
                item[c] = int(r.get(c) or 0)
            sum_days = float(r.get("sum_fix_latency_days") or 0.0)
            resolved = item["resolved_fixes_count"]
            item["sum_fix_latency_days"] = sum_days
            item["avg_fix_latency_days"] = round(sum_days / resolved, 2) if resolved else None
            out.append(item)
        return out

