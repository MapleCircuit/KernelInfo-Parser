"""tests/test_git_reader.py - Unit Tests for GitReader and FilesystemService Version Reading."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from fastapi import HTTPException
from webapp.backend.services.git_reader import GitReader, git_reader
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
        # Ensure process exists
        _ = self.reader.read_file("v3.0", "include/linux/lockd/bind.h")
        self.assertIsNotNone(self.reader._proc)
        # Force-kill subprocess
        if self.reader._proc:
            self.reader._proc.kill()
            self.reader._proc.wait()

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


class TestFilesystemServiceGetFile(unittest.TestCase):
    """Test suite for FilesystemService.get_file with version-specific Git reader."""

    def setUp(self) -> None:
        self.svc = FilesystemService()

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

    def test_get_file_directory(self) -> None:
        """Verify get_file on directory returns directory structure."""
        res = self.svc.get_file("v3.0", "include/linux/lockd")
        self.assertEqual(res["type"], "dir")
        self.assertTrue(res["is_directory"])

    def test_get_file_unindexed(self) -> None:
        """Verify get_file on unindexed file (e.g. Makefile) gracefully serves source code."""
        res = self.svc.get_file("v3.0", "Makefile")
        self.assertEqual(res["type"], "file")
        self.assertIn("VERSION = 3", res["content"])

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


if __name__ == "__main__":
    unittest.main()
