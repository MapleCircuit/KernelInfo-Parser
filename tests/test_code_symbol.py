"""Tests for `.agents/scripts/code_symbol.py`."""

import contextlib
import importlib.util
import io
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("code_symbol", ROOT / ".agents/scripts/code_symbol.py")
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)

SOURCE = textwrap.dedent('''\
    """mod"""
    LIMIT = 5
    _PRIVATE = 1

    def top(a, b=2) -> int:
        """Add things.

        More.
        """
        return a + b

    def _hidden():
        pass

    class Box(Base):
        """A box."""
        size = 3

        @staticmethod
        def make(x):
            return Box()

        def _internal(self):
            pass

        class Inner:
            def deep(self):
                return 1
    ''')


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cs.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class CodeSymbol(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.f = self.dir / "mod.py"
        self.f.write_text(SOURCE)
        (self.dir / "__pycache__").mkdir()
        (self.dir / "__pycache__" / "junk.py").write_text("def junk(): pass\n")

    def test_source_with_line_numbers(self):
        rc, out, _ = run(str(self.f), "top")
        self.assertEqual(rc, 0)
        self.assertIn("top 5-10", out.splitlines()[0])
        self.assertIn("5  def top(a, b=2) -> int:", out)
        self.assertIn("return a + b", out)

    def test_method_decorator_assign_and_nested(self):
        rc, out, _ = run(str(self.f), "Box.make", "LIMIT", "Box.Inner.deep", "--lines-only")
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), ["Box.make: 19-21", "LIMIT: 2-2", "Box.Inner.deep: 27-28"])

    def test_bare_method_name_resolves(self):
        rc, out, _ = run(str(self.f), "deep", "--lines-only")
        self.assertEqual((rc, out.strip()), (0, "Box.Inner.deep: 27-28"))

    def test_signature_mode(self):
        rc, out, _ = run(str(self.f), "top", "Box", "--signature")
        self.assertIn("def top(a, b=2) -> int  # Add things.", out)
        self.assertIn("class Box(Base)  # A box.", out)

    def test_not_found_suggests(self):
        rc, _, err = run(str(self.f), "Box.mak")
        self.assertEqual(rc, 1)
        self.assertIn("Did you mean", err)
        self.assertIn("Box.make", err)

    def test_list_public_and_grep(self):
        rc, out, _ = run(str(self.f), "--list", "--public")
        self.assertIn("function 5-10  top", out)
        self.assertNotIn("_hidden", out)
        self.assertNotIn("_internal", out)
        rc, out, _ = run(str(self.f), "--list", "--grep", "deep")
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_list_directory_skips_pycache(self):
        rc, out, _ = run(str(self.dir), "--list")
        self.assertIn("mod.py", out)
        self.assertNotIn("junk", out)

    def test_find_across_repo(self):
        rc, out, _ = run("--find", "BaseDBEngine")
        self.assertEqual(rc, 0)
        self.assertIn("db_engine/base.py", out)
        rc, _, err = run("--find", "NoSuchSymbolAnywhereXyz")
        self.assertEqual(rc, 1)

    def test_smoke_db_engine_base(self):
        rc, out, _ = run(str(ROOT / "db_engine/base.py"), "BaseDBEngine.select_rows", "--lines-only")
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"BaseDBEngine\.select_rows: \d+-\d+")


if __name__ == "__main__":
    unittest.main()
