#!/usr/bin/env python3
"""api_surface.py - generate the compact public API listings `db_engine/API_SURFACE.md` and `cache_engine/API_SURFACE.md`.

Usage:
    python3 .agents/scripts/api_surface.py --write     # regenerate both files
    python3 .agents/scripts/api_surface.py --check     # exit 1 when a file is stale

Each listing is built with `ast` from the source (nothing is imported): public classes, their public methods and
dataclass / NamedTuple fields, public functions, with the signature and the first docstring line. No line numbers, so
the files only go stale when a public signature or docstring line changes. Read one body with
`python3 .agents/scripts/code_symbol.py <file.py> <Symbol>`; semantics are in `db_engine/DB_API.md` /
`cache_engine/CACHE_API.md`.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import code_symbol as cs  # noqa: E402

# (file, symbols or None for every public one, members: "all" | "init" | "none")
Entry = tuple[str, "list[str] | None", str]

SURFACES: dict[str, dict] = {
    "db_engine/API_SURFACE.md": {
        "title": "db_engine public API surface",
        "intro": (
            "Generated listing of the public `db_engine` API (`self` omitted). Semantics, spec primitives and the SQL-habit "
            "table: `db_engine/DB_API.md`. Method body: `python3 .agents/scripts/code_symbol.py <file.py> <Class.method>`."
        ),
        "entries": [
            ("db_engine/__init__.py", ["register_engine", "available_engines", "default_socket_paths", "get_db_engine"], "all"),
            ("db_engine/base.py", None, "all"),
            ("db_engine/pool.py", ["configure_pool", "get_db_reader", "PoolConfigError", "ReadAPI", "ReadSession"], "all"),
            ("db_engine/read_intents.py", None, "all"),
            ("db_engine/spec.py", None, "all"),
            ("db_engine/errors.py", None, "all"),
            ("db_engine/foreign.py", None, "all"),
            ("db_engine/sqlite_db.py", ["SQLiteDB"], "init"),
            ("db_engine/mock_db.py", ["MockDB"], "init"),
        ],
    },
    "cache_engine/API_SURFACE.md": {
        "title": "cache_engine public API surface",
        "intro": (
            "Generated listing of the public `cache_engine` API (`self` omitted). Semantics: `cache_engine/CACHE_API.md`. "
            "Method body: `python3 .agents/scripts/code_symbol.py <file.py> <Class.method>`."
        ),
        "entries": [
            ("cache_engine/__init__.py", ["cache_root", "register_cache_engine", "available_cache_engines",
                                           "configured_engine_name", "get_cache_engine"], "all"),
            ("cache_engine/base.py", None, "all"),
            ("cache_engine/errors.py", None, "all"),
            ("cache_engine/sqlite_cache.py", ["SQLiteCacheEngine"], "init"),
            ("cache_engine/ram_cache.py", ["RAMCacheEngine"], "init"),
        ],
    },
}

DOC_MAX = 90


def _sig(sym: cs.Sym, drop_self: bool) -> str:
    n = sym.node
    args = ast.unparse(n.args)
    if drop_self:
        for lead in ("self, ", "cls, "):
            if args.startswith(lead):
                args = args[len(lead):]
                break
        else:
            if args in ("self", "cls"):
                args = ""
    out = f"def {n.name}({args})"
    if n.returns is not None:
        out += " -> " + ast.unparse(n.returns)
    return out


def _doc(sym: cs.Sym) -> str:
    d = cs.doc_line(sym)
    if len(d) > DOC_MAX:
        d = d[: DOC_MAX - 3].rstrip() + "..."
    return f"  # {d}" if d else ""


def _fields(cls: ast.ClassDef) -> list[str]:
    out = []
    for node in cls.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and not node.target.id.startswith("_"):
            f = f"{node.target.id}: {ast.unparse(node.annotation)}"
            if node.value is not None:
                f += f" = {ast.unparse(node.value)}"
            out.append(f)
    return out


def render_file(rel: str, names: list[str] | None, members: str) -> list[str]:
    path = ROOT / rel
    src, tree = cs.parse_file(path)
    syms = cs.collect(tree)
    lines: list[str] = []
    top = [s for s in syms if "." not in s.qname]
    for s in top:
        if names is not None and s.qname not in names:
            continue
        if s.kind == "function":
            if cs.is_public(s):
                lines.append(_sig(s, False) + _doc(s))
        elif s.kind == "class":
            if not cs.is_public(s):
                continue
            lines.append(cs.signature(s, src.splitlines()) + _doc(s))
            flds = _fields(s.node)
            if flds and members != "none":
                lines.append("    fields: " + "; ".join(flds))
            if members == "none":
                continue
            for m in syms:
                if m.kind != "method" or not m.qname.startswith(s.qname + ".") or m.qname.count(".") != s.qname.count(".") + 1:
                    continue
                short = m.qname.rsplit(".", 1)[-1]
                if members == "init":
                    if short != "__init__":
                        continue
                elif short.startswith("_"):
                    continue
                lines.append("    " + _sig(m, True) + _doc(m))
        elif s.kind == "assign" and names is None and s.qname.startswith("CAP_"):
            lines.append(f"{s.qname}")
    return lines


def render(target: str) -> str:
    cfg = SURFACES[target]
    out = [f"# {cfg['title']}", "", cfg["intro"], "",
           f"Generated by `python3 .agents/scripts/api_surface.py --write` from the source; do not edit.", ""]
    for rel, names, members in cfg["entries"]:
        body = render_file(rel, names, members)
        if not body:
            continue
        out += [f"## {rel}", "", "```python", *body, "```", ""]
    return "\n".join(out).rstrip("\n") + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in ("--write", "--check"):
        print(__doc__)
        return 2
    rc = 0
    for target in SURFACES:
        new = render(target)
        p = ROOT / target
        old = p.read_text(encoding="utf-8") if p.exists() else None
        if argv[0] == "--write":
            if old != new:
                p.write_text(new, encoding="utf-8")
                print(f"updated {target}")
        elif old != new:
            print(f"stale: {target} (run: python3 .agents/scripts/api_surface.py --write)")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
