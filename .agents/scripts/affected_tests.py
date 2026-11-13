#!/usr/bin/env python3
"""affected_tests.py - Which test modules cover a set of changed files (static, no imports executed).

Usage:
    python3 .agents/scripts/affected_tests.py core/foo.py webapp/backend/services/bar.py   # explicit paths
    python3 .agents/scripts/affected_tests.py                                              # working tree vs HEAD (+ untracked)
    python3 .agents/scripts/affected_tests.py --base origin/main                           # working tree vs REF
    python3 .agents/scripts/affected_tests.py --explain | --json

Prints one test module name per line (`tests.test_x`, `unit_test.test_y`). Exit code 0 always; a global change
prints every test module and a `WARNING: full suite` line on stderr.

Mapping rules:
  1. A changed test module maps to itself.
  2. Static import graph: every repo `.py` file is `ast`-parsed (imports at any depth, relative imports resolved,
     importing `a.b.c` also reaches `a/__init__.py` and `a/b/__init__.py`); a test is affected when a changed
     file is in the transitive import closure of the test.
  3. Name heuristic: a changed `<stem>.py` affects `test_<stem>`, `test_<stem>_*`, `test_webapp_<stem>*` (a
     trailing `_service`/`_ast` of the stem is also tried without it).
  4. Text mention: a test whose source mentions the changed file's repo-relative path, or (non-Python files) its
     basename or its directory name, is affected (fixtures, snapshots, frontend assets, scripts run by path).
  5. Global changes fall back to the FULL suite: any file in GLOBAL_FILES, or any changed `.py` file whose
     dependents cover at least GLOBAL_FRACTION of all test modules.
  6. Files that no rule maps (docs, config) affect nothing.
"""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKIP_TOP_DIRS = {"repos", "tool", ".git", "node_modules"}
TEST_DIRS = ("tests", "unit_test")

# Helpers every (or nearly every) test depends on: a change runs the full suite.
GLOBAL_FILES = frozenset({
    "core/DBLayout.py",
    "core/globalstuff.py",
    "db_engine/base.py",
    "pipeline/state.py",
    "tests/__init__.py",
    "tests/db_fixtures.py",
    "unit_test/__init__.py",
    "unit_test/harness.py",
    "unit_test/runner.py",
    "config.json",
})
GLOBAL_FRACTION = 0.6


def _git(args: list[str], root: Path) -> list[str]:
    out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False).stdout
    return [line for line in out.splitlines() if line]


def repo_files(root: Path = ROOT) -> list[str]:
    """Tracked plus untracked-not-ignored files, skipping `repos/`, `tool/`."""
    files = _git(["ls-files", "-co", "--exclude-standard"], root)
    return sorted({f for f in files if f.split("/", 1)[0] not in SKIP_TOP_DIRS and (root / f).is_file()})


def changed_files(base: str | None = None, root: Path = ROOT) -> list[str]:
    """Repo-relative paths changed in the working tree vs `base` (default HEAD), untracked and deleted included."""
    changed = set(_git(["diff", "--name-only", "--no-renames", base or "HEAD"], root))
    changed |= set(_git(["ls-files", "--others", "--exclude-standard"], root))
    return sorted(f for f in changed if f.split("/", 1)[0] not in SKIP_TOP_DIRS)


def _module_name(rel: str) -> str:
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def is_test_module(rel: str) -> bool:
    top, _, name = rel.partition("/")
    return top in TEST_DIRS and "/" not in name and name.startswith("test_") and name.endswith(".py")


class ImportGraph:
    """`file -> (strong, weak) files it imports` over the repo's Python files."""

    def __init__(self, files: list[str], root: Path = ROOT):
        self.root = root
        self.py = [f for f in files if f.endswith(".py")]
        self.by_module = {_module_name(f): f for f in self.py}
        self.edges: dict[str, tuple[set[str], set[str]]] = {f: self._imports(f) for f in self.py}

    def _resolve(self, dotted: str) -> tuple[list[str], list[str]]:
        """`(strong, weak)` files reached by importing `dotted`: the module itself is strong, its parent packages'
        `__init__` files are weak (counted as dependencies, but their own imports are not followed, because
        `__init__` files re-export widely and would link every test to every module)."""
        parts = dotted.split(".")
        hits = [self.by_module.get(".".join(parts[:i])) for i in range(1, len(parts) + 1)]
        strong = [hits[-1]] if hits[-1] else []
        return strong, [h for h in hits[:-1] if h]

    def _imports(self, rel: str) -> tuple[set[str], set[str]]:
        try:
            tree = ast.parse((self.root / rel).read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):
            return set(), set()
        pkg = _module_name(rel).split(".")
        if not rel.endswith("__init__.py"):
            pkg = pkg[:-1]
        strong: set[str] = set()
        weak: set[str] = set()

        def add(dotted: str) -> None:
            st, wk = self._resolve(dotted)
            strong.update(st)
            weak.update(wk)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base_parts = pkg[: len(pkg) - (node.level - 1)] if node.level - 1 <= len(pkg) else []
                    base = ".".join(base_parts + ([node.module] if node.module else []))
                else:
                    base = node.module or ""
                if base:
                    add(base)
                    for alias in node.names:
                        add(f"{base}.{alias.name}")
        strong.discard(rel)
        weak.discard(rel)
        return strong, weak - strong

    def closure(self, rel: str) -> set[str]:
        seen: set[str] = set()
        expanded: set[str] = set()
        stack = [rel]
        while stack:
            node = stack.pop()
            if node in expanded:
                continue
            expanded.add(node)
            strong, weak = self.edges.get(node, (set(), set()))
            seen |= weak
            for dep in strong:
                seen.add(dep)
                stack.append(dep)
        return seen


def _name_candidates(rel: str) -> list[str]:
    stem = Path(rel).stem
    if stem == "__init__":
        stem = Path(rel).parent.name
    stems = {stem}
    for suffix in ("_service", "_ast", "_store"):
        if stem.endswith(suffix) and len(stem) > len(suffix):
            stems.add(stem[: -len(suffix)])
    return sorted(stems)


def _name_match(test_rel: str, rel: str) -> bool:
    tname = Path(test_rel).stem[len("test_"):]
    for s in _name_candidates(rel):
        for cand in (s, f"webapp_{s}"):
            if tname == cand or tname.startswith(cand + "_"):
                return True
    return False


def affected(changed: list[str], root: Path = ROOT) -> dict:
    """`{"modules": [...], "full": bool, "reasons": {path: why}}` for `changed` repo-relative paths."""
    files = repo_files(root)
    tests = [f for f in files if is_test_module(f)]
    all_modules = sorted(_module_name(t) for t in tests)
    graph = ImportGraph(files, root)
    closures = {t: graph.closure(t) for t in tests}
    sources = {t: (root / t).read_text(encoding="utf-8", errors="replace") for t in tests}

    hit: set[str] = set()
    reasons: dict[str, str] = {}
    full_reason = None
    for rel in changed:
        rel = rel.replace("\\", "/")
        rel = rel[2:] if rel.startswith("./") else rel
        is_py = rel.endswith(".py")
        if rel in GLOBAL_FILES:
            full_reason = full_reason or f"{rel} is a global test helper"
            reasons[rel] = "global"
            continue
        mine: set[str] = set()
        if is_py and is_test_module(rel) and (root / rel).exists():
            mine.add(rel)
        for t in tests:
            if rel in closures[t]:
                mine.add(t)
            elif is_py and _name_match(t, rel):
                mine.add(t)
            else:
                needles = [rel]
                if is_py:
                    if not (root / rel).exists():  # deleted module: its dotted name may still be mentioned
                        needles.append(_module_name(rel))
                else:
                    needles.append(Path(rel).name)
                    parent = Path(rel).parent.name
                    if len(parent) >= 5:
                        needles.append(parent)
                if any(n and n in sources[t] for n in needles):
                    mine.add(t)
        if is_py and len(mine) >= GLOBAL_FRACTION * len(tests) and len(tests) > 5:
            full_reason = full_reason or f"{rel} is imported by {len(mine)}/{len(tests)} test modules"
            reasons[rel] = "global"
            continue
        reasons[rel] = f"{len(mine)} test module(s)"
        hit |= mine
    if full_reason:
        return {"modules": all_modules, "full": True, "reasons": reasons, "why_full": full_reason}
    return {"modules": sorted(_module_name(t) for t in hit), "full": False, "reasons": reasons, "why_full": None}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Test modules affected by changed files.")
    ap.add_argument("paths", nargs="*", help="Changed repo-relative paths (default: git working tree vs --base)")
    ap.add_argument("--base", default=None, help="Git ref to diff the working tree against (default HEAD)")
    ap.add_argument("--json", action="store_true", help="Print {modules, full, reasons} as JSON")
    ap.add_argument("--explain", action="store_true", help="Print the per-file mapping on stderr")
    args = ap.parse_args(argv)
    paths = [str(Path(p).resolve().relative_to(ROOT)) if Path(p).is_absolute() else p for p in args.paths]
    changed = paths or changed_files(args.base)
    result = affected(changed)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    if args.explain:
        for path, why in result["reasons"].items():
            print(f"  {path}: {why}", file=sys.stderr)
    if result["full"]:
        print(f"WARNING: full suite ({result['why_full']})", file=sys.stderr)
    for module in result["modules"]:
        print(module)
    return 0


if __name__ == "__main__":
    sys.exit(main())
