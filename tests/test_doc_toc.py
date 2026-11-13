"""tests/test_doc_toc.py - generated docs stay current: spec TOCs (`toc.py`), API surfaces (`api_surface.py`), section references."""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".agents" / "scripts"


def _load(name: str):
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(SCRIPTS))


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args], cwd=ROOT, capture_output=True, text=True)


KNOWN_SPECS = (
    "core/API.md", "webapp/API.md", "webapp/API_ENDPOINTS.md", "webapp/API_FRONTEND.md", "db_engine/DB_API.md",
    "table_engine/TE_API.md", "cache_engine/CACHE_API.md", "parser/c_ast/CAST_API.md", "parser/asm_ast/ASM_API.md",
    "parser/kconfig_ast/KCONFIG_API.md", "parser/rust_ast/RUST_API.md", "parser/git_ast/GIT_API.md",
    "parser/maintainer_ast/MAINTAINER_API.md", "parser/man_ast/MAN_API.md", "parser/raw_ast/RAW_AST_API.md",
)


class TestSpecTocs(unittest.TestCase):
    def test_every_toc_is_current(self) -> None:
        r = _run("toc.py", "--check", "all")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_known_specs_have_a_toc(self) -> None:
        toc = _load("toc")
        found = {str(p.relative_to(ROOT)) for p in toc.spec_files()}
        self.assertEqual(sorted(set(KNOWN_SPECS) - found), [], "spec without '## Table of Contents' block")

    def test_no_hand_written_line_ranges_outside_tocs(self) -> None:
        pat = re.compile(r"\[L\d+-L\d+\]|\b[Ll]ines? \d+\s*[–-]\s*\d+")
        bad = []
        files = subprocess.run(["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True).stdout.split()
        for rel in files:
            if rel.startswith("repos/") or not (ROOT / rel).is_file():
                continue
            in_toc = False
            for n, line in enumerate((ROOT / rel).read_text(encoding="utf-8").splitlines(), 1):
                if line.startswith("<!-- toc:begin"):
                    in_toc = True
                elif line.startswith("<!-- toc:end"):
                    in_toc = False
                elif not in_toc and pat.search(line):
                    bad.append(f"{rel}:{n}")
        self.assertEqual(bad, [], "line numbers drift; reference a section (spec_section.py) or a symbol (code_symbol.py)")

    def test_toc_script_roundtrip(self) -> None:
        toc = _load("toc")
        text = "# T\n\n## Table of Contents\nold\n\n---\n\n## 1. One\n\nx\n\n### 1.1. Sub\n\ny\n\n## 2. Two\n"
        out = toc.render(text)
        self.assertEqual(toc.render(out), out)
        self.assertIn("- 1. One [L", out)
        self.assertIn("  - 1.1. Sub [L", out)
        first = re.search(r"- 1\. One \[L(\d+)-L(\d+)\]", out)
        lines = out.split("\n")
        self.assertTrue(lines[int(first.group(1)) - 1].startswith("## 1. One"))


class TestSectionReferences(unittest.TestCase):
    """`<spec>.md §N.N` and `spec_section.py <spec> <N.N>` references in docs must resolve to a heading."""

    FILE = r"((?:[\w.-]+/)*[\w.-]*API[\w.-]*\.md)"
    REF_FILE = re.compile(r"`" + FILE + r"`\)?[ (]+((?:§\s*\d+[A-Za-z]?(?:\.\d+)*(?:\s*(?:,|and|/|;)\s*)?)+)")
    REF_CMD = re.compile(r"spec_section\.py\s+" + FILE + r"\s+[\"']?(\d+[A-Za-z]?(?:\.\d+)*)[\"']?[\s`)]")
    NUM = re.compile(r"§\s*(\d+[A-Za-z]?(?:\.\d+)*)")

    def _numbers(self, spec: Path) -> set[str]:
        nums = set()
        in_code = False
        for line in spec.read_text(encoding="utf-8").splitlines():
            if re.match(r"^\s*(```|~~~)", line):
                in_code = not in_code
            m = None if in_code else re.match(r"^#{1,6}\s+§?\s*(\d+[A-Za-z]?(?:\.\d+)*)(?=[.\s:]|$)", line)
            if m:
                nums.add(m.group(1))
        return nums

    def _refs(self, text: str):
        for m in self.REF_FILE.finditer(text):
            for n in self.NUM.findall(m.group(2)):
                yield m.group(1), n
        for m in self.REF_CMD.finditer(text):
            yield m.group(1), m.group(2)

    def test_references_resolve(self) -> None:
        files = subprocess.run(["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True).stdout.split()
        cache: dict[Path, set[str]] = {}
        missing = []
        checked = 0
        for rel in files:
            doc = ROOT / rel
            if rel.startswith("repos/") or doc.is_symlink() or not doc.is_file():
                continue
            for spec_rel, num in self._refs(doc.read_text(encoding="utf-8")):
                spec = ROOT / spec_rel
                if not spec.is_file():
                    spec = doc.parent / spec_rel
                if not spec.is_file():
                    continue  # basename or example path: not resolvable unambiguously
                checked += 1
                nums = cache.setdefault(spec, self._numbers(spec))
                if num not in nums:
                    missing.append(f"{rel}: {spec_rel} section {num}")
        self.assertGreater(checked, 20)
        self.assertEqual(missing, [], "section reference without a matching heading (spec_section.py would not find it)")

    def test_spec_section_resolves_numeric_reference(self) -> None:
        r = _run("spec_section.py", "core/API.md", "4", "--lines-only")
        self.assertEqual(r.returncode, 0, r.stderr)
        start, end = map(int, r.stdout.split())
        self.assertLess(start, end)


class TestApiSurface(unittest.TestCase):
    def test_surfaces_are_current(self) -> None:
        r = _run("api_surface.py", "--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_surface_covers_core_names(self) -> None:
        db = (ROOT / "db_engine/API_SURFACE.md").read_text(encoding="utf-8")
        for name in ("class BaseDBEngine", "def insert_rows", "def transaction", "class ReadSession", "class Select",
                     "class DuplicateKeyError", "def get_db_engine", "def open_foreign_sqlite"):
            self.assertIn(name, db)
        cache = (ROOT / "cache_engine/API_SURFACE.md").read_text(encoding="utf-8")
        for name in ("class BaseCacheEngine", "class CacheSpace", "class CacheStore", "class Eq", "class CacheError",
                     "def get_cache_engine"):
            self.assertIn(name, cache)

    def test_surface_readable_in_full_under_the_guard(self) -> None:
        guard = _load("pre_tool_guard")
        for rel in ("db_engine/API_SURFACE.md", "cache_engine/API_SURFACE.md"):
            decision, reason = guard.evaluate_read_file({"path": rel}, ROOT, overrides=set())
            self.assertEqual(decision, "allow", f"{rel}: {reason}")


if __name__ == "__main__":
    unittest.main()
