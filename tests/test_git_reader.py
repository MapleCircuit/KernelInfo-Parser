"""tests/test_git_reader.py - Unit Tests for GitReader and FilesystemService Version Reading."""
import unittest

from tests.db_fixtures import requires_live_db
from concurrent.futures import ThreadPoolExecutor
from fastapi import HTTPException
from webapp.backend.services.git_reader import READERS_PER_REPO, GitReader, git_reader
from webapp.backend.services.filesystem_service import FilesystemService


class TestGitReader(unittest.TestCase):
    """Test suite for persistent git cat-file reader."""

    def setUp(self) -> None:
        self.reader = git_reader

    def test_read_small_file(self) -> None:
        """Verify reading a small header file returns exact blob bytes."""
        data, is_dir, sha1 = self.reader.read_file("v3.0", "include/linux/lockd/bind.h")
        self.assertFalse(is_dir)
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(len(data), 1305)
        self.assertEqual(sha1, "fbc48f898521c1a24492c8eb782c12301e341568")
        self.assertIn(b"LINUX_LOCKD_BIND_H", data)

    def test_read_large_file(self) -> None:
        """Verify reading a large header file returns exact blob bytes."""
        data, is_dir, sha1 = self.reader.read_file("v3.0", "include/linux/sched.h")
        self.assertFalse(is_dir)
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(len(data), 80877)
        self.assertEqual(sha1, "14a6c7b545de5bf3ad51dea50d5d484cfc8bfbb5")
        self.assertIn(b"struct task_struct", data)

    def test_read_directory_tree(self) -> None:
        """Verify reading a directory path reports is_dir=True."""
        data, is_dir, sha1 = self.reader.read_file("v3.0", "include/linux/lockd")
        self.assertTrue(is_dir)
        self.assertIsNone(data)
        self.assertIsNotNone(sha1)

    def test_read_missing_file(self) -> None:
        """Verify reading a nonexistent file returns None and False."""
        data, is_dir, sha1 = self.reader.read_file("v3.0", "nonexistent/path/missing.c")
        self.assertFalse(is_dir)
        self.assertIsNone(data)
        self.assertIsNone(sha1)

    def test_process_restart_resilience(self) -> None:
        """Verify GitReader seamlessly recovers when underlying process is terminated."""
        # Ensure process exists (the pool's reader of the active repository)
        _ = self.reader.read_file("v3.0", "include/linux/lockd/bind.h")
        readers = self.reader._group().readers
        self.assertTrue(any(r._proc is not None for r in readers))
        # Force-kill every subprocess of the pool
        for inner in readers:
            if inner._proc:
                inner._proc.kill()
                inner._proc.wait()

        # Subsequent read should restart process and succeed
        data, is_dir, sha1 = self.reader.read_file("v3.0", "include/linux/lockd/bind.h")
        self.assertFalse(is_dir)
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(len(data), 1305)

    def test_concurrent_reads(self) -> None:
        """Verify thread-safety across concurrent workers reading various files."""
        targets = [
            ("v3.0", "include/linux/lockd/bind.h"),
            ("v3.0", "include/linux/sched.h"),
            ("v3.0", "Makefile"),
            ("v3.0", "include/linux/lockd"),
        ] * 10

        def worker(item: tuple[str, str]) -> tuple[bytes | None, bool]:
            rev, path = item
            raw, is_dir, _ = self.reader.read_file(rev, path)
            return (raw, is_dir)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(worker, targets))

        self.assertEqual(len(results), len(targets))
        for raw, is_dir in results:
            self.assertTrue(is_dir or (raw is not None and len(raw) > 0))
        # Concurrent demand grows the pool, never past its bound
        self.assertLessEqual(len(self.reader._group().readers), READERS_PER_REPO)


class TestFilesystemServiceGetFile(unittest.TestCase):
    """Test suite for FilesystemService.get_file with version-specific Git reader."""

    def setUp(self) -> None:
        self.svc = FilesystemService()

    @requires_live_db("reads the live database")
    def test_get_file_small_and_big(self) -> None:
        """Verify get_file retrieves version-specific source for small and large files."""
        res_small = self.svc.get_file("v3.0", "include/linux/lockd/bind.h")
        self.assertEqual(res_small["type"], "file")
        self.assertEqual(res_small["version"], "v3.0")
        self.assertIn("LINUX_LOCKD_BIND_H", res_small["content"])

        res_big = self.svc.get_file("v3.0", "include/linux/sched.h")
        self.assertEqual(res_big["type"], "file")
        self.assertEqual(res_big["version"], "v3.0")
        self.assertIn("struct task_struct", res_big["content"])

    @requires_live_db("reads the live database")
    def test_get_file_directory(self) -> None:
        """Verify get_file on directory returns directory structure."""
        res = self.svc.get_file("v3.0", "include/linux/lockd")
        self.assertEqual(res["type"], "dir")
        self.assertTrue(res["is_directory"])

    @requires_live_db("reads the live database")
    def test_get_file_unindexed(self) -> None:
        """Verify get_file on unindexed file (e.g. Makefile) gracefully serves source code."""
        res = self.svc.get_file("v3.0", "Makefile")
        self.assertEqual(res["type"], "file")
        self.assertIn("VERSION = 3", res["content"])

    @requires_live_db("reads the live database")
    def test_get_file_missing_404(self) -> None:
        """Verify nonexistent file raises 404."""
        with self.assertRaises(HTTPException) as ctx:
            self.svc.get_file("v3.0", "nonexistent/does_not_exist.c")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_get_file_path_traversal_400(self) -> None:
        """Verify path traversal attempt raises 400."""
        with self.assertRaises(HTTPException) as ctx:
            self.svc.get_file("v3.0", "../../../etc/passwd")
        self.assertEqual(ctx.exception.status_code, 400)


class TestGitBlameService(unittest.TestCase):
    """Test suite for GitService blame annotation and router endpoints."""

    def setUp(self) -> None:
        from webapp.backend.services.git_service import GitService
        self.svc = GitService()

    @requires_live_db("reads the live database")
    def test_get_blame_success_and_schema(self) -> None:
        """Verify git blame produces the keys the webapp frontend reads, on every line."""
        res = self.svc.get_blame("v3.0", "include/linux/lockd/bind.h")
        self.assertIn("lines", res)
        self.assertGreater(len(res["lines"]), 0)
        self.assertEqual([e["line"] for e in res["lines"]], list(range(1, len(res["lines"]) + 1)))

        for entry in res["lines"]:
            # orig_line / source_path come from the porcelain header (H9, webapp/API.md section 4.6); previous_sha /
            # previous_path only when the blamed commit has a parent
            required = {"line", "commit_hash", "author_name", "author_email", "date", "subject", "orig_line", "source_path"}
            self.assertEqual(set(entry) - {"previous_sha", "previous_path"}, required)
            # --porcelain names a commit's author once; every later line of it still carries it
            self.assertNotEqual(entry["author_name"], "Unknown")
            self.assertGreater(entry["date"], 0)
        self.assertEqual([e["line"] for e in res["lines"]], list(range(1, len(res["lines"]) + 1)))

    @requires_live_db("reads the live database")
    def test_get_blame_router_scoped(self) -> None:
        """Verify the commits router's blame route proxies blame retrieval."""
        from webapp.backend.routers.commits import get_blame
        res = get_blame("v3.0", "include/linux/lockd/bind.h")
        self.assertIn("lines", res)
        self.assertGreater(len(res["lines"]), 0)

    def test_get_blame_path_traversal(self) -> None:
        """Verify blame with invalid/path-traversal path raises 400."""
        with self.assertRaises(HTTPException) as ctx:
            self.svc.get_blame("v3.0", "../../../etc/passwd")
        self.assertEqual(ctx.exception.status_code, 400)

    @requires_live_db("reads the live database")
    def test_get_blame_file_with_form_feeds(self) -> None:
        """Verify files with form feed characters (e.g. COPYING:75) blame every line without missing entries."""
        res = self.svc.get_blame("v3.10", "COPYING")
        self.assertIn("lines", res)
        self.assertEqual(len(res["lines"]), 356)
        self.assertEqual([e["line"] for e in res["lines"]], list(range(1, 357)))
        line75 = next((e for e in res["lines"] if e["line"] == 75), None)
        self.assertIsNotNone(line75)
        self.assertEqual(line75["line"], 75)
        self.assertNotEqual(line75["commit_hash"], "")
        self.assertNotEqual(line75["author_name"], "Unknown")


if __name__ == "__main__":
    unittest.main()

