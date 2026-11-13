#!/usr/bin/env python3
"""code_symbol.py - Read Python code by symbol instead of grep + line slices.

Uses `ast` only (the target module is never imported).

Usage:
    # Source of symbols, prefixed with "start-end" and numbered lines
    python3 .agents/scripts/code_symbol.py db_engine/base.py BaseDBEngine.select_rows other_func
    # Only the line ranges (for Read offset/limit) / only signature + first docstring line
    python3 .agents/scripts/code_symbol.py main.py run_unit_tests --lines-only
    python3 .agents/scripts/code_symbol.py db_engine/base.py BaseDBEngine.upsert --signature
    # List symbols of a file or directory (recursive), optionally public-only / filtered
    python3 .agents/scripts/code_symbol.py db_engine/base.py --list --public --grep select
    python3 .agents/scripts/code_symbol.py db_engine --list
    # Find where a symbol is defined across the repo (git ls-files '*.py', skips repos/ and tool/)
    python3 .agents/scripts/code_symbol.py --find BaseDBEngine [path ...]

Symbols: functions, classes, methods (`Class.method`, nested `Outer.Inner.m`) and
module-level assignments. A bare method name (`select_rows`) resolves when unambiguous.
"""

import argparse
import ast
import difflib
import subprocess
import sys
from pathlib import Path

SKIP_TOP_DIRS = {"repos", "tool"}


class Sym:
    __slots__ = ("qname", "kind", "start", "end", "node")

    def __init__(self, qname, kind, start, end, node):
        self.qname, self.kind, self.start, self.end, self.node = qname, kind, start, end, node


def _span(node) -> tuple[int, int]:
    start = node.lineno
    decos = getattr(node, "decorator_list", None)
    if decos:
        start = min(start, min(d.lineno for d in decos))
    return start, node.end_lineno


def _targets(node) -> list[str]:
    if isinstance(node, ast.Assign):
        out = []
        for t in node.targets:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    out.append(n.id)
        return out
    if isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def collect(tree: ast.AST) -> list[Sym]:
    syms: list[Sym] = []

    def visit(body, prefix, in_class):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = prefix + node.name
                s, e = _span(node)
                kind = "method" if in_class else "function"
                syms.append(Sym(q, kind, s, e, node))
            elif isinstance(node, ast.ClassDef):
                q = prefix + node.name
                s, e = _span(node)
                syms.append(Sym(q, "class", s, e, node))
                visit(node.body, q + ".", True)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                if not in_class or prefix:
                    for name in _targets(node):
                        syms.append(Sym(prefix + name, "attr" if in_class else "assign",
                                        node.lineno, node.end_lineno, node))
            elif isinstance(node, (ast.If, ast.Try, ast.With)) and not in_class:
                # top-level conditional definitions
                for sub in ("body", "orelse", "finalbody"):
                    visit(getattr(node, sub, []) or [], prefix, in_class)
                for h in getattr(node, "handlers", []) or []:
                    visit(h.body, prefix, in_class)

    visit(tree.body, "", False)
    return syms


def parse_file(path: Path):
    src = path.read_text(encoding="utf-8", errors="replace")
    return src, ast.parse(src, filename=str(path))


def signature(sym: Sym, lines: list[str]) -> str:
    n = sym.node
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        prefix = "async def " if isinstance(n, ast.AsyncFunctionDef) else "def "
        sig = prefix + n.name + "(" + ast.unparse(n.args) + ")"
        if n.returns is not None:
            sig += " -> " + ast.unparse(n.returns)
        return sig
    if isinstance(n, ast.ClassDef):
        bases = [ast.unparse(b) for b in n.bases] + [ast.unparse(k) for k in n.keywords]
        return f"class {n.name}" + (f"({', '.join(bases)})" if bases else "")
    first = lines[sym.start - 1].strip() if sym.start - 1 < len(lines) else sym.qname
    return first if len(first) <= 100 else first[:97] + "..."


def doc_line(sym: Sym) -> str:
    n = sym.node
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        d = ast.get_docstring(n)
        if d:
            return d.strip().splitlines()[0]
    return ""


def is_public(sym: Sym) -> bool:
    return not any(part.startswith("_") and not (part.startswith("__") and part.endswith("__"))
                   for part in sym.qname.split("."))


def resolve(name: str, syms: list[Sym]) -> list[Sym]:
    exact = [s for s in syms if s.qname == name]
    if exact:
        return exact
    # bare method / nested name: unambiguous suffix match
    return [s for s in syms if s.qname.endswith("." + name)]


def suggest(name: str, syms: list[Sym]) -> list[str]:
    names = sorted({s.qname for s in syms})
    close = difflib.get_close_matches(name, names, n=5, cutoff=0.5)
    short = {s.qname.rsplit(".", 1)[-1]: s.qname for s in syms}
    for m in difflib.get_close_matches(name, list(short), n=5, cutoff=0.6):
        if short[m] not in close:
            close.append(short[m])
    return close[:6]


def cmd_symbols(path: Path, names: list[str], mode: str) -> int:
    try:
        src, tree = parse_file(path)
    except (OSError, SyntaxError) as exc:
        print(f"error: cannot parse {path}: {exc}", file=sys.stderr)
        return 2
    lines = src.splitlines()
    syms = collect(tree)
    rc = 0
    for name in names:
        found = resolve(name, syms)
        if not found:
            msg = f"error: symbol '{name}' not found in {path}."
            sg = suggest(name, syms)
            if sg:
                msg += " Did you mean: " + ", ".join(sg) + "?"
            else:
                msg += f" Use --list to see the {len(syms)} symbols."
            print(msg, file=sys.stderr)
            rc = 1
            continue
        if len(found) > 1 and len({s.qname for s in found}) > 1:
            print(f"error: '{name}' is ambiguous in {path}: "
                  + ", ".join(sorted({s.qname for s in found})), file=sys.stderr)
            rc = 1
            continue
        for s in found:
            if mode == "lines":
                print(f"{s.qname}: {s.start}-{s.end}")
            elif mode == "signature":
                d = doc_line(s)
                print(f"{s.qname}  [{s.start}-{s.end}]  {signature(s, lines)}" + (f"  # {d}" if d else ""))
            else:
                print(f"# {path}:{s.qname} {s.start}-{s.end}")
                width = len(str(s.end))
                for i in range(s.start, s.end + 1):
                    print(f"{i:>{width}}  {lines[i - 1]}")
                print()
    return rc


def iter_py(target: Path):
    if target.is_file():
        yield target
        return
    for p in sorted(target.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        yield p


def cmd_list(target: Path, public: bool, pattern: str | None) -> int:
    if not target.exists():
        print(f"error: {target} does not exist", file=sys.stderr)
        return 2
    multi = target.is_dir()
    for p in iter_py(target):
        try:
            src, tree = parse_file(p)
        except (OSError, SyntaxError):
            print(f"# {p}: unparseable", file=sys.stderr)
            continue
        lines = src.splitlines()
        rows = []
        for s in collect(tree):
            if public and not is_public(s):
                continue
            if pattern and pattern.lower() not in s.qname.lower():
                continue
            row = f"{s.kind:<8} {s.start}-{s.end}  {s.qname}"
            if s.kind in ("function", "method", "class"):
                row += f"  | {signature(s, lines)}"
                d = doc_line(s)
                if d:
                    row += f"  # {d}"
            rows.append(row)
        if multi and rows:
            print(f"## {p}")
        if not multi or rows:
            print("\n".join(rows))
    return 0


def repo_py_files(paths: list[str]) -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "*.py"], capture_output=True, text=True, check=True).stdout
        files = [Path(x) for x in out.splitlines() if x]
    except Exception:
        files = [p for p in Path(".").rglob("*.py")]
    files = [f for f in files if f.parts and f.parts[0] not in SKIP_TOP_DIRS and "__pycache__" not in f.parts]
    if paths:
        roots = [Path(p) for p in paths]
        files = [f for f in files if any(f == r or r in f.parents for r in roots)]
    return files


def cmd_find(name: str, paths: list[str]) -> int:
    hits = 0
    last = name.rsplit(".", 1)[-1]
    for f in repo_py_files(paths):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if last not in text:
            continue
        try:
            tree = ast.parse(text, filename=str(f))
        except SyntaxError:
            continue
        lines = text.splitlines()
        for s in resolve(name, collect(tree)):
            print(f"{f}:{s.start}-{s.end}  {s.kind}  {signature(s, lines)}")
            hits += 1
    if not hits:
        print(f"error: symbol '{name}' not defined in any tracked .py file"
              + (f" under {', '.join(paths)}" if paths else "") + ".", file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read Python code by symbol (ast-based).")
    ap.add_argument("items", nargs="*", help="<file.py|dir> <Symbol>...  |  with --find: <Symbol> [path ...]")
    ap.add_argument("--lines-only", action="store_true", help="print only start-end per symbol")
    ap.add_argument("--signature", action="store_true", help="print signature + first docstring line")
    ap.add_argument("--list", action="store_true", help="list symbols of a file or directory")
    ap.add_argument("--public", action="store_true", help="with --list: skip _private symbols")
    ap.add_argument("--grep", metavar="PATTERN", help="with --list: case-insensitive name filter")
    ap.add_argument("--find", metavar="SYMBOL", help="find where SYMBOL is defined across the repo")
    a = ap.parse_args(argv)

    if a.find:
        return cmd_find(a.find, a.items)
    if not a.items:
        ap.print_usage(sys.stderr)
        return 2
    target = Path(a.items[0])
    if a.list:
        return cmd_list(target, a.public, a.grep)
    if not target.is_file():
        print(f"error: {target} is not a file (use --list for directories, --find for repo-wide lookup).",
              file=sys.stderr)
        return 2
    if len(a.items) < 2:
        print("error: give at least one symbol (or --list).", file=sys.stderr)
        return 2
    mode = "lines" if a.lines_only else "signature" if a.signature else "source"
    return cmd_symbols(target, a.items[1:], mode)


if __name__ == "__main__":
    sys.exit(main())
