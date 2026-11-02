"""webapp/backend/services/symbol_service.py - Semantic Symbol Indexing, XRefs & AST Container Inspection."""
from __future__ import annotations
from typing import Any
from fastapi import HTTPException
from core.SymbolRows import rows_of_targets_sql
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import (
    safe_decode,
    get_version_info,
)
from webapp.backend.security.sql import escape_like, sanitize_like_query
from webapp.backend.services.arch_layout_service import arch_layout_service
from webapp.backend.services.tree_resolver import (
    DEFAULT_DEFINITION_ARCH,
    best_path,
    definition_arch,
    definition_rank,
    git_tree_resolver,
    path_rank,
)

# Symbol search (search_symbols) ranks distinct names first, then fetches their definitions in small
# batches: ordering the m_symbol_def view itself by length sorted every prefix match of every indexed
# version (minutes for a one-letter prefix). Names come from the name indexes, NAME_PAGE at a time.
NAME_PAGE = 400
# Names of the first definition batch; each later batch doubles, up to MAX_NAME_BATCH
FIRST_NAME_BATCH = 8
MAX_NAME_BATCH = 128
# Most definition rows one batch reads (symbols of every indexed version share m_symbol_def, so a batch
# can match many blobs that are not in the requested tree)
SEARCH_CANDIDATES = 30000
# Name pages scanned before giving up on filling the answer (names absent from the requested tree)
MAX_NAME_PAGES = 8
# Deepest m_ast_container expansion one request may ask for (see get_ast_tree)
MAX_AST_TREE_DEPTH = 12
# Tree positions (serialized nodes) one answer may carry. Shared subtrees are built once but still
# serialize at every position they occupy, so the response grows multiplicatively with depth even
# when few nodes are distinct: the depth cap alone does not bound the body, this does.
MAX_AST_TREE_NODES = 20000
# AST ids per IN list while get_ast_tree fetches a level of the tree
AST_TREE_CHUNK = 1000
# Most references one xref answer returns when the caller names no limit
MAX_XREF_REFS = 5000
# Most target ASTs one xref answer looks references up for. Only reached at a revision without an in-tree
# definition, where every indexed definition of the name is a target (one per content of the defining file)
MAX_XREF_TARGETS = 64


def format_symbol_type(type_name: str) -> str:
    """Format internal AST type names into user-friendly display labels."""
    if type_name == "C_enumequal":
        return "EnumConstant"
    return type_name


def _symbol_asts(def_rows: list[dict[str, Any]], symbol_name: str) -> list[int]:
    """The ASTs of `def_rows` that are the symbol itself, deduplicated, at most MAX_XREF_TARGETS.

    A marker definition (`EXPORT_SYMBOL(sym)`, an initcall) records the symbol's name against the AST of the
    macro, which is content-addressed and therefore the same node for every symbol the kernel marks that way
    (180k rows share 4 ASTs here). Its references are the references of all of them, so matching `m_ast.name`
    against the symbol's name is what keeps them out of this symbol's cross-references.
    """
    return list(dict.fromkeys(
        d["ast_id"] for d in def_rows
        if d["ast_id"] and safe_decode(d.get("ast_name")) == symbol_name
    ))[:MAX_XREF_TARGETS]


class SymbolService:
    """Service providing symbol searching, definitions, cross-reference index (XRef), and AST container trees."""

    @staticmethod
    def _scope(cursor: Any, version_name: str) -> tuple[int | None, str, dict[int, list[str]]]:
        """Resolve (vid, vname, {blob_id: [paths]}) for any revision: tag, branch or arbitrary commit.

        The Git tree is authoritative (works for commits without a checkpoint snapshot, even commits
        that were never ingested); m_bridge_checkpoint is the fallback when Git is unavailable.
        """
        vid, vname = get_version_info(cursor, version_name)
        rev = vname if vid is not None else version_name
        blob_paths = git_tree_resolver.blob_paths(cursor, rev)
        if blob_paths is None:
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")
            cursor.execute(
                """
                SELECT bc.blob_id, fn.fname
                FROM m_bridge_checkpoint bc
                JOIN m_file_name fn ON bc.fnid = fn.fnid
                WHERE bc.commit_id = %s;
                """,
                (vid,),
            )
            blob_paths = {}
            for r in cursor.fetchall():
                blob_paths.setdefault(r["blob_id"], []).append(safe_decode(r["fname"]))
        return vid, rev, blob_paths

    def search_symbols(self, version_name: str, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
        """Symbol definitions of the revision whose name starts with `query`: the exact name first, then
        shorter names, then by name."""
        clean_q = sanitize_like_query(query)
        if not clean_q:
            return []

        with get_db_cursor() as cursor:
            _, _, blob_paths = self._scope(cursor, version_name)
            cap = max(1, min(limit, 200))
            picked: list[dict[str, Any]] = []
            batch_size = FIRST_NAME_BATCH
            after: tuple[int, str] | None = None
            for _ in range(MAX_NAME_PAGES):
                names = self._name_page(cursor, f"{clean_q}%", after)
                for i in range(0, len(names), batch_size):
                    picked.extend(self._definitions_in_tree(cursor, names[i:i + batch_size], blob_paths))
                    batch_size = min(batch_size * 2, MAX_NAME_BATCH)
                    if len(picked) >= cap:
                        break
                if len(picked) >= cap or len(names) < NAME_PAGE:
                    break
                after = (len(names[-1].encode()), names[-1])

            results = []
            for r in picked[:cap]:
                path = r["file_path"]
                results.append({
                    "def_id": r["def_id"],
                    "name": r["name"],
                    "symbol_name": r["name"],
                    "type_id": r["type_id"],
                    "type_name": format_symbol_type(safe_decode(r["type_name"])),
                    "fid": r["fid"],
                    "file_path": path,
                    "file_name": path,
                    "line_s": r["line_s"],
                    "line_e": r["line_e"],
                    "ast_id": r["ast_id"],
                })
            return results

    @staticmethod
    def _name_page(cursor: Any, like: str, after: tuple[int, str] | None) -> list[str]:
        """Next NAME_PAGE distinct definition names matching `like`, by (byte length, name), after `after`.

        Each table holding definition names (shared per tag, or per blob) is read through its name index
        and only its distinct names are sorted; the two lists merge in Python. Being ordered by the same
        key, the union's first NAME_PAGE names are among the first NAME_PAGE of each table.
        """
        cond, args = "", [like]
        if after is not None:
            cond = " AND (LENGTH(name) > %s OR (LENGTH(name) = %s AND name > %s))"
            args += [after[0], after[0], after[1]]
        names: set[str] = set()
        for table in ("m_tag_def", "m_blob_symbol_def"):
            cursor.execute(
                f"SELECT name FROM {table} WHERE name LIKE %s{cond} GROUP BY name ORDER BY LENGTH(name), name LIMIT %s;",
                (*args, NAME_PAGE),
            )
            names.update(safe_decode(r["name"]) for r in cursor.fetchall())
        return sorted(names, key=lambda n: (len(n.encode()), n))[:NAME_PAGE]

    @staticmethod
    def _definitions_in_tree(cursor: Any, names: list[str], blob_paths: dict[int, list[str]]) -> list[dict[str, Any]]:
        """Definitions of `names` whose blob is in the revision's tree, in the order of `names`, then by path and line."""
        if not names:
            return []
        rank = {n: i for i, n in enumerate(names)}
        cursor.execute(
            f"""
            SELECT s.def_id, s.name, s.type_id, t.name AS type_name, s.blob_id AS fid, s.line_s, s.line_e, s.ast_id
            FROM m_symbol_def s
            JOIN m_type_descriptor t ON s.type_id = t.type_id
            WHERE s.name IN ({", ".join(["%s"] * len(names))})
            LIMIT %s;
            """,
            (*names, SEARCH_CANDIDATES),
        )
        rows = []
        for r in cursor.fetchall():
            paths = blob_paths.get(r["fid"])
            if not paths:
                continue
            r["name"] = safe_decode(r["name"])
            r["file_path"] = paths[0]
            rows.append(r)
        rows.sort(key=lambda r: (rank.get(r["name"], len(rank)), r["file_path"], r["line_s"] or 0))
        return rows

    @staticmethod
    def _prefer_reachable(rows: list[dict[str, Any]], blob_paths: dict[int, list[str]], rev: str, arch_id: str,
                          from_file: str, scope: Any, bits: int | None) -> None:
        """Put first the definitions the compilation of `from_file` reaches through its #include chain for
        ARCH=<arch_id> (arch_layout_service.reachable); prototypes stay last and weak definitions after
        strong ones. Marks rows `visible` (rows are left untouched when nothing is reached)."""
        reached = arch_layout_service.reachable(rev, arch_id, from_file)
        if not reached:
            return
        for r in rows:
            hit = next((p for p in blob_paths.get(r["fid"]) or () if p in reached), None)
            r["visible"] = hit is not None
            if hit:
                r["fname"] = hit

        def key(r: dict[str, Any]) -> tuple:
            proto, group, bits_penalty, link_rank, path, line = definition_rank(r["fname"], r.get("linkage"), r["line_s"], scope, bits)
            return (proto, 1 if link_rank == 1 else 0, 0 if r["visible"] else 1, group, bits_penalty, link_rank, path, line)

        rows.sort(key=key)

    @staticmethod
    def _kconfig_detail(vname: str, name: str, arch_id: str) -> dict[str, Any] | None:
        """Symbol detail of a Kconfig symbol (CONFIG_ prefix optional) in the ARCH's model, else None."""
        clean_name = name[7:] if name.startswith("CONFIG_") else name
        try:
            from webapp.backend.services.kconfig_service import KconfigService
            k_detail = KconfigService().get_symbol_detail(vname, clean_name, arch_id)
        except Exception:  # noqa: BLE001 - not a Kconfig symbol of this revision and ARCH
            return None
        if not k_detail or not k_detail.get("file_path"):
            return None
        return {
            "version": vname,
            "name": name,
            "symbol_name": name,
            "symbol": name,
            "type_name": "kconfig",
            "file_path": k_detail["file_path"],
            "line": k_detail["line_s"],
            "line_s": k_detail["line_s"],
            "line_e": k_detail["line_e"],
            "definition": {
                "def_id": 0,
                "ast_id": 0,
                "tag_id": 0,
                "type_id": 88,
                "type_name": "kconfig",
                "fid": None,
                "file": k_detail["file_path"],
                "file_path": k_detail["file_path"],
                "line_s": k_detail["line_s"],
                "line_e": k_detail["line_e"],
            },
            "is_kconfig": True,
            "kconfig": k_detail,
            "arch": arch_id,
        }

    def get_symbol_detail(self, version_name: str, name: str, arch: str = DEFAULT_DEFINITION_ARCH,
                          from_file: str | None = None) -> dict[str, Any]:
        """The definition of a symbol in the revision's tree.

        Among several definitions the one for `arch` wins (see `definition_rank`); with `from_file` (the
        file the symbol is used in), the definitions its #include chain reaches for that ARCH come first
        (`_prefer_reachable`). Kconfig symbols are looked up in that ARCH's model. `definitions` lists
        every in-tree definition in rank order. References are not part of the answer: get_symbol_xref()
        computes them, only for the views that show them.
        """
        with get_db_cursor() as cursor:
            _, vname, blob_paths = self._scope(cursor, version_name)
            arch_id, srcarch, bits = definition_arch(vname, arch)

            # A Kconfig configuration symbol (CONFIG_NET, CONFIG_SMP)
            if name.startswith("CONFIG_"):
                k_res = self._kconfig_detail(vname, name, arch_id)
                if k_res is not None:
                    return k_res

            # Authoritative definition: one whose blob is in this revision's tree, ranked for the ARCH
            cursor.execute(
                """
                SELECT s.def_id, s.ast_id, s.name, s.type_id, t.name AS type_name, s.blob_id AS fid, s.line_s, s.line_e, s.tag_id, s.linkage
                FROM m_symbol_def s
                JOIN m_type_descriptor t ON s.type_id = t.type_id
                WHERE s.name = %s
                ORDER BY s.def_id ASC;
                """,
                (name,),
            )
            def_rows = cursor.fetchall()
            in_tree = [r for r in def_rows if r["fid"] in blob_paths]
            for r in in_tree:
                r["fname"] = best_path(blob_paths[r["fid"]], srcarch, bits)
            in_tree.sort(key=lambda r: definition_rank(r["fname"], r.get("linkage"), r["line_s"], srcarch, bits))
            if from_file and in_tree:
                self._prefer_reachable(in_tree, blob_paths, vname, arch_id, from_file, srcarch, bits)
            s_row = in_tree[0] if in_tree else (def_rows[0] if def_rows else None)
            if s_row is not None and not in_tree:
                s_row["fname"] = None
            if not s_row:
                # A Kconfig symbol named without its CONFIG_ prefix (NET)
                k_res = self._kconfig_detail(vname, name, arch_id)
                if k_res is not None:
                    return k_res
                raise HTTPException(status_code=404, detail=f"Symbol '{name}' not found in version '{version_name}'.")

            return {
                "version": vname,
                "name": name,
                "symbol_name": name,
                "symbol": name,
                "type_name": format_symbol_type(safe_decode(s_row["type_name"])),
                "file_path": safe_decode(s_row["fname"]),
                "line": s_row["line_s"],
                "line_s": s_row["line_s"],
                "definition": {
                    "def_id": s_row["def_id"],
                    "ast_id": s_row["ast_id"],
                    "tag_id": s_row["tag_id"],
                    "type_id": s_row["type_id"],
                    "type_name": format_symbol_type(safe_decode(s_row["type_name"])),
                    "fid": s_row["fid"],
                    "file": safe_decode(s_row["fname"]),
                    "line_s": s_row["line_s"],
                    "line_e": s_row["line_e"],
                },
                "definitions": [
                    {
                        "file": safe_decode(r["fname"]),
                        "line_s": r["line_s"],
                        "line_e": r["line_e"],
                        "type_name": format_symbol_type(safe_decode(r["type_name"])),
                        "ast_id": r["ast_id"],
                        "linkage": r.get("linkage"),
                        "visible": r.get("visible"),
                    }
                    for r in in_tree[:50]
                ],
                "arch": arch_id,
                "from_file": from_file,
            }

    def get_symbol_xref(self, version_name: str, symbol_name: str, limit: int | None = None,
                        arch: str = DEFAULT_DEFINITION_ARCH) -> dict[str, Any]:
        """Definitions and references of a symbol; each reference carries its role badge (`type`).

        `definitions` are the definitions in this revision's tree, ranked for `arch` (see `definition_rank`);
        `definition` is the first one. References are looked up for every AST that *is* the symbol, in the
        tree or not, so a revision whose copy of the defining file was never parsed still resolves them.
        """
        with get_db_cursor() as cursor:
            _, vname, blob_paths = self._scope(cursor, version_name)
            arch_id, srcarch, bits = definition_arch(vname, arch)

            # Resolve symbol definition to ast_id. a.name tells the symbol's own AST from the AST of a marker
            # definition (EXPORT_SYMBOL(sym), an initcall): that one is the macro's content-addressed node,
            # shared by every symbol it marks, so it is shown but never a reference target (see ref_targets)
            cursor.execute(
                """
                SELECT s.ast_id, s.type_id, t.name AS type_name, s.line_s, s.line_e, s.blob_id, s.linkage,
                       a.name AS ast_name
                FROM m_symbol_def s
                JOIN m_type_descriptor t ON s.type_id = t.type_id
                LEFT JOIN m_ast a ON a.ast_id = s.ast_id
                WHERE s.name = %s;
                """,
                (symbol_name,),
            )
            def_rows = cursor.fetchall()
            in_tree = [d for d in def_rows if d["blob_id"] in blob_paths]
            # Only definitions of this tree are definitions of this revision: an out-of-tree blob sits at no
            # path here, so it has nothing to show and nothing to jump to
            defs = sorted(
                (
                    {
                        "file": best_path(blob_paths.get(d["blob_id"]), srcarch, bits),
                        "line_s": d["line_s"],
                        "line_e": d["line_e"],
                        "type_name": format_symbol_type(safe_decode(d["type_name"])),
                        "ast_id": d["ast_id"],
                        "linkage": d["linkage"],
                    }
                    for d in in_tree
                ),
                key=lambda d: definition_rank(d["file"], d["linkage"], d["line_s"], srcarch, bits),
            )
            # References are resolved against the AST of the definition the referencing blob was parsed
            # with, so without an in-tree definition every indexed AST of the symbol is a candidate target
            ref_targets = _symbol_asts(in_tree, symbol_name) or _symbol_asts(def_rows, symbol_name)

            # Fallback to m_ast lookup if no explicit m_symbol_def record exists
            if not def_rows:
                cursor.execute(
                    """
                    SELECT a.ast_id, a.type_id, t.name AS type_name, bt.blob_id, bt.line_s, bt.line_e
                    FROM m_ast a
                    JOIN m_type_descriptor t ON a.type_id = t.type_id
                    JOIN m_tag tg ON a.ast_id = tg.ast_id
                    JOIN m_bridge_tag bt ON tg.tag_id = bt.tag_id
                    WHERE a.name = %s
                    LIMIT 200;
                    """,
                    (symbol_name,),
                )
                tag_rows = cursor.fetchall()
                ref_targets = list(dict.fromkeys(r["ast_id"] for r in tag_rows if r["ast_id"]))[:MAX_XREF_TARGETS]
                tag_rows = [r for r in tag_rows if r["blob_id"] in blob_paths]
                tag_rows.sort(key=lambda r: (path_rank(best_path(blob_paths.get(r["blob_id"]), srcarch, bits), srcarch, bits), r["line_s"] or 0))
                for f_row in tag_rows[:5]:
                    defs.append({
                        "file": best_path(blob_paths.get(f_row["blob_id"]), srcarch, bits),
                        "line_s": f_row["line_s"],
                        "line_e": f_row["line_e"],
                        "type_name": safe_decode(f_row["type_name"]),
                        "ast_id": f_row["ast_id"],
                    })

            ast_ids = ref_targets
            targets = set(ast_ids)
            primary_ast_id = next((d["ast_id"] for d in defs if d["ast_id"] in targets), ast_ids[0] if ast_ids else 0)

            ref_rows = []
            if ast_ids:
                # Key-first form of `m_symbol_ref WHERE ast_id IN (...)` (the view itself takes seconds per target)
                refs_sql, repeat = rows_of_targets_sql("m_symbol_ref", len(ast_ids))
                # The referencing file is the path of r.blob_id in this revision (target_fnid is the target's file);
                # the caller is the AST of the tag the reference sits in
                cursor.execute(
                    f"""
                    SELECT r.role, r.line, r.char_s, r.blob_id AS fid, r.tag_id, a.name AS caller
                    FROM ({refs_sql}) r
                    LEFT JOIN m_tag t ON r.tag_id = t.tag_id
                    LEFT JOIN m_ast a ON a.ast_id = t.ast_id;
                    """,
                    list(ast_ids) * repeat,
                )
                ref_rows = [
                    {**r, "fname": best_path(blob_paths[r["fid"]], srcarch, bits)}
                    for r in cursor.fetchall()
                    if r["fid"] in blob_paths
                ]
                ref_rows.sort(key=lambda r: (r["fname"], r["line"] or 0, r["char_s"] or 0))
                # Always capped: without a limit, a symbol like kmalloc returns tens of thousands
                cap = min(limit, MAX_XREF_REFS) if limit and limit > 0 else MAX_XREF_REFS
                ref_rows = ref_rows[:cap]

            # Reference badges by SymbolRole (Decl, TypeUsage, Call, MemberRef, DeclRef, MacroExpansion)
            role_badges = {1: "Decl", 2: "TypeUsage", 3: "Call", 4: "MemberRef", 5: "DeclRef", 6: "MacroExp"}
            references = [
                {
                    "file": r["fname"],
                    "caller": safe_decode(r["caller"]) if r["caller"] else None,
                    "fid": r["fid"],
                    "line": r["line"],
                    "char_s": r["char_s"],
                    "tag_id": r["tag_id"],
                    "type": role_badges.get(r["role"], "Ref"),
                }
                # Grouped by role (unknown roles with DeclRef), file/line order within a role
                for r in sorted(ref_rows, key=lambda r: r["role"] if r["role"] in role_badges else 5)
            ]

            return {
                "version": vname,
                "arch": arch_id,
                "symbol": symbol_name,
                "name": symbol_name,
                "ast_id": primary_ast_id,
                "definitions": defs,
                "definition": defs[0] if defs else None,
                # The index knows this symbol, but no definition of it sits in this revision's tree: the
                # revision's copy of the defining file was never parsed (its references still resolve)
                "definition_unindexed": bool(def_rows) and not defs,
                "references": references,
            }

    def get_ast_tree(self, ast_id: int, depth: int = 3) -> dict[str, Any]:
        """Recursive m_ast_container hierarchy inspection with tag_id annotations and batched queries."""
        # AST nodes are content-addressed, so one subtree is referenced by many parents. Without a
        # bound and without memoizing subtrees, a deep request re-expands every shared subtree once
        # per path through it: queries and response size grow exponentially in the depth.
        depth = max(0, min(int(depth), MAX_AST_TREE_DEPTH))
        with get_db_cursor() as cursor:
            node_cache: dict[int, dict[str, Any]] = {}
            # ast_id -> [(child ast_id, priority)] of every node expanded so far
            children_of: dict[int, list[tuple[int, int]]] = {}
            # (ast_id, remaining depth) -> (subtree, positions it serializes to)
            subtree_cache: dict[tuple[int, int], tuple[dict[str, Any], int]] = {}
            # Positions left: the answer never serializes more than MAX_AST_TREE_NODES nodes
            budget = [MAX_AST_TREE_NODES]

            def fetch_batch_nodes(ast_ids: list[int]) -> None:
                missing = list(dict.fromkeys(aid for aid in ast_ids if aid not in node_cache))
                for i in range(0, len(missing), AST_TREE_CHUNK):
                    chunk = missing[i:i + AST_TREE_CHUNK]
                    placeholders = ",".join(["%s"] * len(chunk))
                    cursor.execute(
                        f"""
                        SELECT a.ast_id, a.name, a.type_id, t.name AS type_name,
                               (SELECT tag_id FROM m_tag WHERE ast_id = a.ast_id LIMIT 1) AS tag_id
                        FROM m_ast a
                        JOIN m_type_descriptor t ON a.type_id = t.type_id
                        WHERE a.ast_id IN ({placeholders});
                        """,
                        chunk,
                    )
                    for r in cursor.fetchall():
                        node_cache[r["ast_id"]] = r

            def expand(roots: list[int], levels: int) -> None:
                """Fetch the containers of `roots` and of their descendants `levels` levels down: one query
                per level (and per AST_TREE_CHUNK nodes) instead of one per node. Each node is expanded once,
                at the shallowest level it appears, where it has the most depth left. Stops early once
                MAX_AST_TREE_NODES nodes are known (the budget truncates the answer before using more);
                build_tree expands what it still needs on demand."""
                frontier = [aid for aid in dict.fromkeys(roots) if aid not in children_of]
                for _ in range(levels):
                    if not frontier:
                        return
                    for i in range(0, len(frontier), AST_TREE_CHUNK):
                        chunk = frontier[i:i + AST_TREE_CHUNK]
                        cursor.execute(
                            f"""
                            SELECT c.ast_id, c.ref_ast_id, c.priority
                            FROM m_ast_container c
                            WHERE c.ast_id IN ({",".join(["%s"] * len(chunk))})
                            ORDER BY c.ast_id, c.priority ASC;
                            """,
                            chunk,
                        )
                        for r in cursor.fetchall():
                            children_of.setdefault(r["ast_id"], []).append((r["ref_ast_id"], r["priority"]))
                    for aid in frontier:
                        children_of.setdefault(aid, [])
                    found = [child for aid in frontier for child, _ in children_of[aid]]
                    fetch_batch_nodes(found)
                    if len(node_cache) >= MAX_AST_TREE_NODES:
                        return
                    frontier = [aid for aid in dict.fromkeys(found) if aid not in children_of]

            fetch_batch_nodes([ast_id])
            expand([ast_id], depth)

            def build_tree(curr_ast_id: int, curr_depth: int) -> tuple[dict[str, Any], int]:
                """The subtree under curr_ast_id, built fresh, and the number of positions it serializes to.
                Every node built charges one position to the budget; a memoized subtree reused under
                another parent serializes again, so its caller charges its whole size."""
                budget[0] -= 1
                node = node_cache.get(curr_ast_id)
                if not node:
                    return ({"ast_id": curr_ast_id, "tag_id": None, "name": "unknown", "type_id": 0,
                             "children": []}, 1)

                tid = node["tag_id"]
                node_dict: dict[str, Any] = {
                    "ast_id": curr_ast_id,
                    "tag_id": int(tid) if tid is not None else None,
                    "name": safe_decode(node["name"]),
                    "type_id": node["type_id"],
                    "type_name": safe_decode(node["type_name"]),
                    "children": [],
                }
                if curr_depth <= 0:
                    subtree_cache[(curr_ast_id, curr_depth)] = (node_dict, 1)
                    return (node_dict, 1)

                if curr_ast_id not in children_of:
                    expand([curr_ast_id], curr_depth)

                size = 1
                # Children in m_ast_container priority order; the first one that does not fit in the
                # budget cuts the rest
                for child_ast_id, _priority in children_of[curr_ast_id]:
                    memo = subtree_cache.get((child_ast_id, curr_depth - 1))
                    if memo is not None:
                        child_tree, child_size = memo
                        if child_size > budget[0]:
                            node_dict["truncated"] = True
                            break
                        budget[0] -= child_size
                    else:
                        if budget[0] < 1:
                            node_dict["truncated"] = True
                            break
                        child_tree, child_size = build_tree(child_ast_id, curr_depth - 1)
                    size += child_size
                    # Suppress tag_id on untagged sub-expressions if inner statement/expression.
                    # A memoized subtree may also be the root or another parent's child, so the
                    # suppression goes on a shallow copy instead of mutating the shared node.
                    if child_tree["type_id"] > 20 and child_tree["tag_id"] is not None:
                        child_tree = {**child_tree, "tag_id": None}
                    node_dict["children"].append(child_tree)
                subtree_cache[(curr_ast_id, curr_depth)] = (node_dict, size)
                return (node_dict, size)

            return build_tree(ast_id, depth)[0]

    @staticmethod
    def _blob_of(cursor: Any, rev: str, path: str | None) -> int | None:
        """Indexed blob_id of `path` in the tree of `rev` (None when absent or never parsed)."""
        clean = (path or "").strip().strip("/")
        tree = git_tree_resolver.list_tree(rev) if clean else None
        sha = tree.get(clean) if tree else None
        meta = git_tree_resolver.map_blob_hashes(cursor, [sha]).get(sha) if sha else None
        return meta[0] if meta else None

    def get_include_symbols(
        self,
        version_name: str,
        ast_id: int,
        tag_id: int | None = None,
        file_path: str | None = None,
        line: int | None = None,
        header: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve imported symbols and target header file path for a CPPro_include AST node."""
        import os
        from core.globalstuff import normalize_repo_path

        with get_db_cursor() as cursor:
            _, version_name, blob_paths = self._scope(cursor, version_name)

            target_ast_id = ast_id or 0
            if target_ast_id <= 0 and tag_id:
                cursor.execute("SELECT ast_id FROM m_tag WHERE tag_id = %s LIMIT 1;", (tag_id,))
                t_row = cursor.fetchone()
                if t_row and t_row["ast_id"]:
                    target_ast_id = t_row["ast_id"]

            # The include directive of the referring file, looked up in that file's blob at this revision
            file_blob = self._blob_of(cursor, version_name, file_path) if target_ast_id <= 0 and file_path else None
            if target_ast_id <= 0 and file_blob and line:
                cursor.execute(
                    """
                    SELECT a.ast_id
                    FROM m_bridge_tag bt
                    JOIN m_tag t ON bt.tag_id = t.tag_id
                    JOIN m_ast a ON t.ast_id = a.ast_id
                    WHERE bt.blob_id = %s AND a.type_id = 78 AND bt.line_s <= %s AND bt.line_e >= %s
                    LIMIT 1;
                    """,
                    (file_blob, line, line),
                )
                f_row = cursor.fetchone()
                if f_row and f_row["ast_id"]:
                    target_ast_id = f_row["ast_id"]

            if target_ast_id <= 0 and file_blob and header:
                clean_hdr = escape_like(header.strip().strip("<>\"' ;"))
                cursor.execute(
                    """
                    SELECT a.ast_id
                    FROM m_bridge_tag bt
                    JOIN m_tag t ON bt.tag_id = t.tag_id
                    JOIN m_ast a ON t.ast_id = a.ast_id
                    WHERE bt.blob_id = %s AND a.type_id = 78 AND a.name LIKE %s
                    LIMIT 1;
                    """,
                    (file_blob, f"%{clean_hdr}%"),
                )
                h_row = cursor.fetchone()
                if h_row and h_row["ast_id"]:
                    target_ast_id = h_row["ast_id"]

            ast_row = None
            if target_ast_id and target_ast_id > 0:
                cursor.execute(
                    """
                    SELECT a.ast_id, a.name, a.type_id, td.name AS type_name
                    FROM m_ast a
                    LEFT JOIN m_type_descriptor td ON a.type_id = td.type_id
                    WHERE a.ast_id = %s
                    LIMIT 1;
                    """,
                    (target_ast_id,),
                )
                ast_row = cursor.fetchone()

            if not ast_row:
                if (target_ast_id and target_ast_id > 0) and not header and not file_path:
                    raise HTTPException(status_code=404, detail=f"Include AST node {target_ast_id} not found")

                # If include AST node is unindexed or queried with fallback parameters, attempt to resolve header target path gracefully
                hdr_file = ""
                if header:
                    from webapp.backend.services.filesystem_service import filesystem_service
                    try:
                        resolved = filesystem_service.resolve_include(version_name, header, current_file=file_path)
                        hdr_file = resolved.get("path", "")
                    except Exception:
                        hdr_file = header.strip().strip("<>\"' ;")

                return {
                    "ast_id": target_ast_id or 0,
                    "include_text": header or "#include",
                    "header_file": hdr_file,
                    "symbols": [],
                }

            raw_include_text = safe_decode(ast_row["name"])

            cursor.execute(
                """
                SELECT fn.fnid, fn.fname
                FROM m_ast_include ai
                JOIN m_file_name fn ON ai.fnid = fn.fnid
                WHERE ai.ast_id = %s
                LIMIT 1;
                """,
                (target_ast_id,),
            )
            inc_row = cursor.fetchone()
            raw_header = safe_decode(inc_row["fname"]) if inc_row else ""
            header_file = normalize_repo_path(raw_header)

            cursor.execute(
                """
                SELECT c.priority, c.type_id, td.name AS type_name, ra.ast_id AS sym_ast_id, ra.name AS sym_name
                FROM m_ast_container c
                LEFT JOIN m_type_descriptor td ON c.type_id = td.type_id
                LEFT JOIN m_ast ra ON c.ref_ast_id = ra.ast_id
                WHERE c.ast_id = %s
                ORDER BY c.priority ASC;
                """,
                (target_ast_id,),
            )
            cont_rows = cursor.fetchall()

            symbol_names = [safe_decode(r["sym_name"]) for r in cont_rows if r["sym_name"]]

            # Each symbol's definition in this revision's tree: in the header itself, then in its directory
            def_map = {}
            if symbol_names:
                format_strings = ",".join(["%s"] * len(symbol_names))
                header_dir = (os.path.dirname(header_file) + "/") if header_file else ""
                cursor.execute(
                    f"""
                    SELECT d.name, d.blob_id AS fid, d.line_s
                    FROM m_symbol_def d
                    WHERE d.name IN ({format_strings});
                    """,
                    tuple(symbol_names),
                )
                ranked: dict[str, tuple] = {}
                for d_row in cursor.fetchall():
                    paths = blob_paths.get(d_row["fid"])
                    if not paths:
                        continue
                    d_name = safe_decode(d_row["name"])
                    path = header_file if header_file in paths else paths[0]
                    rank = (0 if path == header_file else 1 if header_dir and path.startswith(header_dir) else 2, d_row["line_s"] or 0)
                    if d_name not in ranked or rank < ranked[d_name]:
                        ranked[d_name] = rank
                        def_map[d_name] = {
                            "fid": d_row["fid"],
                            "line_s": d_row["line_s"],
                            "def_file": path,
                        }

            def _get_category(t_name: str) -> str:
                if not t_name:
                    return "Symbol"
                tl = t_name.lower()
                if "enumequal" in tl or "enumconstant" in tl:
                    return "EnumConstant"
                if "struct" in tl:
                    return "Struct"
                if "union" in tl:
                    return "Union"
                if "enum" in tl:
                    return "Enum"
                if "proto" in tl or "func" in tl:
                    return "Function"
                if "typedef" in tl:
                    return "Typedef"
                if "define" in tl or "macro" in tl:
                    return "Macro"
                if "extern" in tl or "var" in tl:
                    return "Variable"
                return "Symbol"

            symbols_list = []
            for r in cont_rows:
                priority = r["priority"]
                type_id = r["type_id"]
                t_name = safe_decode(r["type_name"])
                sym_ast_id = r["sym_ast_id"]
                sym_name = safe_decode(r["sym_name"])

                def_info = def_map.get(sym_name)
                symbols_list.append({
                    "priority": priority,
                    "ast_id": sym_ast_id,
                    "name": sym_name,
                    "type_id": type_id,
                    "type_name": t_name,
                    "category": _get_category(t_name),
                    "def_file": def_info["def_file"] if def_info else None,
                    "def_line_s": def_info["line_s"] if def_info else None,
                })

            return {
                "ast_id": target_ast_id,
                "include_text": raw_include_text,
                "header_file": header_file,
                "symbols": symbols_list,
            }

