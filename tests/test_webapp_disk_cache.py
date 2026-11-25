"""tests/test_webapp_disk_cache.py - Disk cache of derived data (webapp/API.md section 2.14)."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.backend.services import disk_cache
from webapp.backend.services.disk_cache import DiskCache, fingerprint


class TestDiskCache(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"KERNELINFO_CACHE_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_round_trip_and_hashed_names(self) -> None:
        cache = DiskCache("unit-test")
        self.assertIsNone(cache.get("missing"))
        self.assertTrue(cache.put("linux|v3.0|anything goes", {"a": [1, 2, 3], "b": "é"}))
        self.assertEqual(cache.get("linux|v3.0|anything goes"), {"a": [1, 2, 3], "b": "é"})
        files = list((Path(self.tmp.name) / "unit-test").glob("*.json.gz"))
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^[0-9a-f]{40}\.json\.gz$")
        # Hex keys keep their name (the Kconfig model cache predates the helper)
        cache.put("ab" * 20, [1])
        self.assertTrue((Path(self.tmp.name) / "unit-test" / f"{'ab' * 20}.json.gz").is_file())

    def test_invalid_namespace(self) -> None:
        for bad in ("../x", "", "A/B", "x" * 80):
            with self.assertRaises(ValueError):
                DiskCache(bad)

    def test_atomic_write_leaves_no_partial_file(self) -> None:
        cache = DiskCache("unit-test")
        cache.put("k", {"v": 1})
        with patch("webapp.backend.services.disk_cache.os.replace", side_effect=OSError("crash")):
            self.assertFalse(cache.put("k", {"v": 2}))
        self.assertEqual(cache.get("k"), {"v": 1})
        leftovers = [p.name for p in (Path(self.tmp.name) / "unit-test").iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        # Unserializable objects are refused, never half written
        self.assertFalse(cache.put("bad", {"x": object()}))
        self.assertIsNone(cache.get("bad"))

    def test_lru_eviction_by_size_and_count(self) -> None:
        cache = DiskCache("unit-test", max_entries=3)
        for i in range(3):
            cache.put(f"k{i}", {"i": i})
            os.utime(cache.path_for(f"k{i}"), (time.time() - 100 + i, time.time() - 100 + i))
        cache.get("k0")  # refreshed: now the most recently used
        cache.put("k3", {"i": 3})
        self.assertIsNotNone(cache.get("k0"))
        self.assertIsNone(cache.get("k1"))
        self.assertIsNotNone(cache.get("k3"))
        small = DiskCache("unit-small", max_bytes=1)
        small.put("a", {"x": "y" * 100})
        self.assertIsNone(small.get("a"))

    def test_global_cap(self) -> None:
        with patch("webapp.backend.services.disk_cache.total_cap_bytes", return_value=200):
            a = DiskCache("ns-a")
            b = DiskCache("ns-b")
            a.put("old", {"pad": os.urandom(64).hex()})
            os.utime(a.path_for("old"), (time.time() - 1000, time.time() - 1000))
            b.put("new", {"pad": os.urandom(64).hex()})
            self.assertIsNone(a.get("old"))
            self.assertIsNotNone(b.get("new"))
        names = {u["name"] for u in disk_cache.usage()}
        self.assertIn("ns-b", names)

    def test_fingerprint_changes_with_source(self) -> None:
        src = Path(self.tmp.name) / "producer.py"
        src.write_text("x = 1\n")
        disk_cache._FINGERPRINTS.clear()
        first = fingerprint(src)
        src.write_text("x = 2\n")
        disk_cache._FINGERPRINTS.clear()
        self.assertNotEqual(first, fingerprint(src))
        self.assertNotEqual(fingerprint(src), fingerprint(src, salt="v2"))

    def test_kconfig_models_keep_their_location(self) -> None:
        from webapp.backend.services import kconfig_service
        self.assertEqual(kconfig_service._MODEL_DISK.directory, Path(self.tmp.name) / "kconfig-models")
        kconfig_service._disk_store("cd" * 20, {"model": {"version": "x"}})
        self.assertTrue((Path(self.tmp.name) / "kconfig-models" / f"{'cd' * 20}.json.gz").is_file())
        self.assertEqual(kconfig_service._disk_load("cd" * 20), {"model": {"version": "x"}})


class TestDiskCacheFaultInjectionAndSyncInvalidation(unittest.TestCase):
    """Verify BE-016 corrupt-gzip/NaN handling and BE-017 parse-dependent cache clearing."""

    def test_corrupt_gzip_unlinked_and_nan_rejected(self) -> None:
        import gzip
        from unittest.mock import patch

        from webapp.backend.services.disk_cache import DiskCache

        with tempfile.TemporaryDirectory() as tmp, patch(
            "webapp.backend.services.disk_cache.cache_root", return_value=Path(tmp)
        ):
            cache = DiskCache("fault")
            self.assertTrue(cache.put("ok", {"v": 1}))
            self.assertFalse(cache.put("nan", {"bad": float("nan")}))

            corrupt_path = cache.path_for("broken")
            corrupt_path.parent.mkdir(parents=True, exist_ok=True)
            # Valid gzip header followed by garbage triggers zlib.error on read
            with gzip.open(corrupt_path, "wb") as fh:
                fh.write(b'{"a": 1}')
            raw = corrupt_path.read_bytes()
            corrupt_path.write_bytes(raw[:12] + b"\xff\xff\x00\x00")
            self.assertTrue(corrupt_path.exists())
            self.assertIsNone(cache.get("broken"))
            self.assertFalse(corrupt_path.exists())

    def test_clear_parse_dependent_caches_clears_secondary_caches(self) -> None:
        from webapp.backend.services import (
            filesystem_service,
            hover_service,
            kapi_diff_service,
            outline_service,
            scope_service,
            sync_service,
        )

        filesystem_service._OBJECT_CACHE["__test__"] = {"x": 1}
        hover_service.hover_service._cache[("sha", "sym", "x86", "")] = {"found": True}
        scope_service.scope_service._models[("v3.0", "x86")] = object()  # type: ignore[assignment]
        scope_service.scope_service._answers[("v3.0", "x86", "CONFIG_X")] = {}
        outline_service.outline_service._cache[("sha", "a.c")] = {}
        kapi_diff_service._CACHE[("a", "b")] = {}

        sync_service.clear_parse_dependent_caches()

        self.assertNotIn("__test__", filesystem_service._OBJECT_CACHE)
        self.assertEqual(len(hover_service.hover_service._cache), 0)
        self.assertEqual(len(scope_service.scope_service._models), 0)
        self.assertEqual(len(scope_service.scope_service._answers), 0)
        self.assertEqual(len(outline_service.outline_service._cache), 0)
        self.assertEqual(len(kapi_diff_service._CACHE), 0)


if __name__ == "__main__":
    unittest.main()
