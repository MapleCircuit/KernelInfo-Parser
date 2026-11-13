"""Tests for `.agents/scripts/pre_tool_guard.py` (rule names, public allowlist, overrides)."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("pre_tool_guard", ROOT / ".agents/scripts/pre_tool_guard.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def check(path, overrides=None):
    return guard.evaluate_read_file({"path": path}, ROOT, overrides=set() if overrides is None else overrides)


class GuardRules(unittest.TestCase):
    def test_internal_db_engine_file_blocked_with_rule_name(self):
        decision, reason = check("db_engine/DBHandling.py")
        self.assertEqual(decision, "deny")
        self.assertIn("[rule: engine_boundary]", reason)
        self.assertIn("guard_overrides.local.json", reason)
        self.assertIn("KIP_GUARD_ALLOW", reason)

    def test_public_api_files_always_allowed(self):
        for name in ("API_SURFACE.md", "base.py", "DB_API.md", "spec.py", "errors.py", "read_intents.py", "__init__.py"):
            if name in ("base.py", "DB_API.md", "read_intents.py"):
                # large files need a slice; boundary rule must not fire
                decision, reason = guard.evaluate_read_file(
                    {"path": f"db_engine/{name}", "StartLine": 1, "EndLine": 50}, ROOT, overrides=set())
            else:
                decision, reason = check(f"db_engine/{name}")
            self.assertEqual(decision, "allow", f"{name}: {reason}")

    def test_override_via_set(self):
        self.assertEqual(check("db_engine/foreign.py", {"engine_boundary"})[0], "allow")
        # the override lifts only the named rule: the 40 KB slice rule still applies
        decision, reason = check("db_engine/sqlite_db.py", {"engine_boundary"})
        self.assertIn("[rule: slice_limit]", reason)

    def test_unaffected_rules_stay_active(self):
        decision, reason = check("Dockerfile", {"engine_boundary"})
        self.assertEqual(decision, "deny")
        self.assertIn("[rule: forbidden_files]", reason)
        decision, reason = check("core/__pycache__/x.pyc", {"engine_boundary"})
        self.assertIn("[rule: forbidden_dirs]", reason)
        decision, reason = check("core/TableHandling.py", {"engine_boundary"})
        self.assertIn("[rule: slice_limit]", reason)
        self.assertEqual(check("core/TableHandling.py", {"slice_limit"})[0], "allow")

    def test_deprecated_stub_rule(self):
        self.assertIn("[rule: deprecated_stub]", check("table_engine/API.md")[1])


class OverrideLoading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "o.json"

    def test_file_override(self):
        self.file.write_text(json.dumps({"allow": ["engine_boundary", "bogus"], "reason": "t"}))
        self.assertEqual(guard.load_overrides(self.file, env={}), {"engine_boundary"})

    def test_env_override(self):
        got = guard.load_overrides(self.file, env={"KIP_GUARD_ALLOW": "engine_boundary, slice_limit,nope"})
        self.assertEqual(got, {"engine_boundary", "slice_limit"})

    def test_missing_file_no_override(self):
        self.assertEqual(guard.load_overrides(self.file, env={}), set())

    def test_invalid_file_no_override(self):
        for text in ("{not json", "[]", '{"allow": "engine_boundary"}', '{"allow": [1, null]}'):
            self.file.write_text(text)
            self.assertEqual(guard.load_overrides(self.file, env={}), set(), text)


if __name__ == "__main__":
    unittest.main()
