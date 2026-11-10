"""tests/test_webapp_frontend_js.py - Runs the frontend's JavaScript unit tests (webapp/frontend/tests).

The suites use Node's built-in runner (`node --test`, `node:test` and `node:assert` only), so the
frontend keeps no build step and no npm dependency. Skipped when Node 18 or newer is not on PATH.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
JS_TESTS = ROOT / "webapp" / "frontend" / "tests"
MIN_NODE = 18


def node_version(node: str) -> int:
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    m = re.match(r"v(\d+)", out.strip())
    return int(m.group(1)) if m else 0


def run_js_tests(test_dir: Path = JS_TESTS, node: str | None = None, timeout: float = 300) -> subprocess.CompletedProcess:
    """`node --test` over every *.test.mjs file below test_dir. Files are listed explicitly: Node 18-20
    take no globs, and Node 22 reads a directory argument as a file."""
    files = sorted(str(p) for p in test_dir.rglob("*.test.mjs"))
    return subprocess.run([node or "node", "--test", "--test-timeout=60000", *files], capture_output=True,
                          text=True, timeout=timeout, cwd=str(ROOT))


class TestFrontendJs(unittest.TestCase):
    def setUp(self) -> None:
        self.node = shutil.which("node")
        if not self.node or node_version(self.node) < MIN_NODE:
            self.skipTest(f"node {MIN_NODE}+ is not installed")

    def test_frontend_js_suites(self) -> None:
        """Every webapp/frontend/tests/*.test.mjs suite passes."""
        self.assertTrue(any(JS_TESTS.rglob("*.test.mjs")), "no JS test files found")
        res = run_js_tests(node=self.node)
        if res.returncode != 0:
            failing = [line for line in res.stdout.splitlines() if line.lstrip().startswith("not ok")]
            self.fail("JavaScript unit tests failed:\n" + "\n".join(failing) + "\n\n" + res.stdout[-6000:] + res.stderr[-2000:])
        self.assertRegex(res.stdout, r"# fail 0")

    def test_bridge_reports_failures(self) -> None:
        """A failing assertion makes the bridge fail (the suite cannot pass vacuously)."""
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "broken.test.mjs").write_text(
                'import { test } from "node:test";\nimport assert from "node:assert/strict";\n'
                'test("broken", () => assert.equal(1, 2));\n')
            res = run_js_tests(Path(tmp), node=self.node, timeout=60)
        self.assertNotEqual(res.returncode, 0)


if __name__ == "__main__":
    unittest.main()
