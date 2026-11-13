"""tests/test_test_runner.py - `unit_test/runner.py` (isolation, timeout, summary) and `.agents/scripts/affected_tests.py`. No database."""
from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from unit_test import runner

ROOT = Path(__file__).resolve().parents[1]


def _load_affected():
    spec = importlib.util.spec_from_file_location("affected_tests", ROOT / ".agents" / "scripts" / "affected_tests.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSuiteRegistration(unittest.TestCase):
    """Every test module on disk is run by `python3 main.py -u` or explicitly excluded (with a reason)."""

    @staticmethod
    def _on_disk() -> set[str]:
        return {f"{d}.{p.stem}" for d in ("tests", "unit_test") for p in sorted((ROOT / d).glob("test_*.py"))}

    def test_every_test_module_is_registered_or_excluded(self) -> None:
        known = set(runner.DEFAULT_MODULES) | set(runner.EXCLUDED_MODULES)
        missing = sorted(self._on_disk() - known)
        self.assertEqual(
            missing, [],
            "test module(s) not run by `main.py -u`: add them to DEFAULT_MODULES in unit_test/runner.py "
            "(or to EXCLUDED_MODULES with a reason)",
        )

    def test_registry_entries_exist_and_are_unique(self) -> None:
        mods = list(runner.DEFAULT_MODULES)
        self.assertEqual(len(mods), len(set(mods)), "duplicate entries in DEFAULT_MODULES")
        on_disk = self._on_disk()
        self.assertEqual(sorted(set(mods) - on_disk), [], "DEFAULT_MODULES entry without a file")
        self.assertEqual(sorted(set(runner.EXCLUDED_MODULES) - on_disk), [], "EXCLUDED_MODULES entry without a file")
        self.assertEqual(sorted(set(mods) & set(runner.EXCLUDED_MODULES)), [], "module is both registered and excluded")
        for name, reason in runner.EXCLUDED_MODULES.items():
            self.assertTrue(reason.strip(), f"exclusion of {name} needs a reason")


class TestAffectedTests(unittest.TestCase):
    """Synthetic mini repo so rules are checked independent of the real tree."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        files = {
            "core/__init__.py": "",
            "core/alpha.py": "from core import beta\n",
            "core/beta.py": "X = 1\n",
            "core/gamma.py": "def f():\n    import core.beta\n",
            "core/lonely.py": "",
            "tests/__init__.py": "",
            "tests/db_fixtures.py": "",
            "tests/test_alpha_flow.py": "from core.alpha import beta\n",
            "tests/test_gamma.py": "import core.gamma\n",
            "tests/test_misc.py": "DATA = 'fixtures/data.json'\n",
            "tests/fixtures/data.json": "{}",
            "docs/readme.md": "x",
        }
        for rel, text in files.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        self.at = _load_affected()

    def mods(self, *paths: str) -> list[str]:
        return self.at.affected(list(paths), self.root)["modules"]

    def test_transitive_import(self) -> None:
        self.assertEqual(self.mods("core/beta.py"), ["tests.test_alpha_flow", "tests.test_gamma"])

    def test_lazy_import_and_direct(self) -> None:
        self.assertEqual(self.mods("core/gamma.py"), ["tests.test_gamma"])

    def test_name_heuristic_and_self(self) -> None:
        self.assertEqual(self.mods("core/alpha.py"), ["tests.test_alpha_flow"])
        self.assertEqual(self.mods("tests/test_misc.py"), ["tests.test_misc"])

    def test_text_mention_of_data_file(self) -> None:
        self.assertEqual(self.mods("tests/fixtures/data.json"), ["tests.test_misc"])

    def test_unmapped_files_affect_nothing(self) -> None:
        self.assertEqual(self.mods("docs/readme.md", "core/lonely.py"), [])

    def test_global_file_runs_everything(self) -> None:
        res = self.at.affected(["tests/db_fixtures.py"], self.root)
        self.assertTrue(res["full"])
        self.assertEqual(res["modules"], ["tests.test_alpha_flow", "tests.test_gamma", "tests.test_misc"])

    def test_widely_imported_file_falls_back_to_full(self) -> None:
        for i in range(6):
            (self.root / "tests" / f"test_w{i}.py").write_text("import core.beta\n")
        res = self.at.affected(["core/beta.py"], self.root)
        self.assertTrue(res["full"])
        self.assertIn("imported by", res["why_full"])

    def test_real_repo_runner_and_script_map_to_their_tests(self) -> None:
        res = _load_affected().affected([".agents/scripts/code_symbol.py"], ROOT)
        self.assertIn("tests.test_code_symbol", res["modules"])
        self.assertFalse(res["full"])


class TestRunner(unittest.TestCase):
    def test_normalize_module(self) -> None:
        self.assertEqual(runner.normalize_module("tests/test_config.py"), "tests.test_config")
        self.assertEqual(runner.normalize_module("test_config"), "tests.test_config")
        self.assertEqual(runner.normalize_module("test_ddl_render"), "unit_test.test_ddl_render")
        self.assertEqual(runner.normalize_module("tests.test_config"), "tests.test_config")

    def test_default_modules_exist(self) -> None:
        for mod in runner.DEFAULT_MODULES:
            self.assertTrue((ROOT / (mod.replace(".", "/") + ".py")).exists(), mod)

    def test_isolated_summary_timeout_and_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "kip_tmp_tests"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("")
            (pkg / "test_ok.py").write_text(textwrap.dedent("""
                import unittest
                class T(unittest.TestCase):
                    def test_a(self): pass
                    @unittest.skip("because")
                    def test_b(self): pass
            """))
            (pkg / "test_bad.py").write_text(textwrap.dedent("""
                import unittest
                class T(unittest.TestCase):
                    def test_fail(self): self.assertEqual(1, 2)
                    def test_err(self): raise ValueError("boom")
            """))
            (pkg / "test_hang.py").write_text("import time\ntime.sleep(60)\n")
            env_old = dict(os.environ)
            os.environ["PYTHONPATH"] = tmp + os.pathsep + os.environ.get("PYTHONPATH", "")
            os.environ["KIP_TEST_LOG_DIR"] = str(Path(tmp) / "logs")
            self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env_old)))
            buf = io.StringIO()
            code = runner.run_isolated(
                ["kip_tmp_tests.test_ok", "kip_tmp_tests.test_bad", "kip_tmp_tests.test_hang"],
                timeout=3, summary=True, out=buf,
            )
            text = buf.getvalue()
            self.assertEqual(code, 1)
            self.assertIn("FAIL kip_tmp_tests.test_bad.T.test_fail", text)
            self.assertIn("ERROR kip_tmp_tests.test_bad.T.test_err", text)
            self.assertIn("ValueError: boom", text)
            self.assertIn("TIMEOUT after 3s", text)
            self.assertIn("ran=4 failures=1 errors=2 skipped=1", text)
            log_line = [ln for ln in text.splitlines() if ln.startswith("full log: ")][0]
            self.assertIn("Traceback", Path(log_line[len("full log: "):]).read_text())

    def test_live_db_marker_honours_env(self) -> None:
        from tests.db_fixtures import requires_live_db

        @requires_live_db("needs db")
        def guarded() -> str:
            return "ran"

        old = os.environ.get(runner.NO_LIVE_DB_ENV)
        os.environ[runner.NO_LIVE_DB_ENV] = "1"
        try:
            with self.assertRaises(unittest.SkipTest) as ctx:
                guarded()
            self.assertIn("needs db", str(ctx.exception))
        finally:
            if old is None:
                os.environ.pop(runner.NO_LIVE_DB_ENV)
            else:
                os.environ[runner.NO_LIVE_DB_ENV] = old


if __name__ == "__main__":
    unittest.main()
