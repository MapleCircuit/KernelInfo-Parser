"""tests/test_duration_profiler.py - Unit Tests for Duration Limiting & Stage Profiler."""
import unittest
import time
from core.globalstuff import G
from core.Profiler import (
    parse_duration,
    PipelineProfiler,
    PipelineStageProfiler,
    StageMetric,
)


class TestDurationProfiler(unittest.TestCase):
    """Test suite verifying duration string parsing, execution deadlines, and stage profiling."""

    def tearDown(self) -> None:
        G.set_execution_deadline(None)
        G.STAGE_PROFILER = None
        G.PROFILING_ENABLED = False

    def test_parse_duration(self) -> None:
        """Verify parse_duration correctly parses seconds, minutes, hours, and raw numbers."""
        self.assertEqual(parse_duration("30m"), 1800.0)
        self.assertEqual(parse_duration("1800s"), 1800.0)
        self.assertEqual(parse_duration("1h"), 3600.0)
        self.assertEqual(parse_duration("0.5h"), 1800.0)
        self.assertEqual(parse_duration("45"), 45.0)
        self.assertEqual(parse_duration(1800), 1800.0)
        self.assertEqual(parse_duration(45.5), 45.5)
        self.assertIsNone(parse_duration(None))
        self.assertIsNone(parse_duration(""))
        self.assertIsNone(parse_duration("invalid"))

    def test_execution_deadline_mechanics(self) -> None:
        """Verify G.set_execution_deadline, is_deadline_exceeded, and get_time_remaining."""
        self.assertFalse(G.is_deadline_exceeded())
        self.assertIsNone(G.get_time_remaining())

        # Future deadline
        G.set_execution_deadline(60.0)
        self.assertFalse(G.is_deadline_exceeded())
        rem = G.get_time_remaining()
        self.assertIsNotNone(rem)
        self.assertGreater(rem, 50.0)

        # Expired deadline
        G.set_execution_deadline(-1.0)
        self.assertTrue(G.is_deadline_exceeded())
        self.assertEqual(G.get_time_remaining(), 0.0)

        # Clear deadline
        G.set_execution_deadline(None)
        self.assertFalse(G.is_deadline_exceeded())
        self.assertIsNone(G.get_time_remaining())

    def test_stage_profiler_measure_and_report(self) -> None:
        """Verify PipelineStageProfiler tracks stage timings, throughput, and formats report."""
        profiler = PipelineStageProfiler()
        G.STAGE_PROFILER = profiler
        G.PROFILING_ENABLED = True

        with G.measure_stage("Test Git Diff", count=1000, units="files"):
            time.sleep(0.01)

        with G.measure_stage("Test Clang Parser", count=500, units="headers"):
            time.sleep(0.02)

        # Accumulate simulated worker profiler
        worker_p = PipelineProfiler(
            file_path="kernel/fork.c",
            clang_parse_tu_s=0.015,
            clang_tokenize_s=0.003,
            token_processing_s=0.002,
            ast_extraction_s=0.005,
            cs_execute_s=0.002,
            te_db_commit_s=0.001,
            total_elapsed_s=0.028,
        )
        profiler.record_worker_profiler(worker_p)

        report = profiler.format_stage_report(title="TEST STAGE PROFILE")
        self.assertIn("TEST STAGE PROFILE", report)
        self.assertIn("Test Git Diff", report)
        self.assertIn("Test Clang Parser", report)
        self.assertIn("PARALLEL WORKER CPU BREAKDOWN", report)
        self.assertIn("TOP IDENTIFIED BOTTLENECKS", report)
        self.assertIn("files", report)
        self.assertIn("headers", report)

    def test_cli_duration_argument_parsing(self) -> None:
        """Verify main.py arg_handling recognizes --duration and configures G deadline."""
        import sys
        from unittest.mock import patch
        from main import arg_handling

        test_args = ["main.py", "--duration", "30m"]
        with patch.object(sys, "argv", test_args):
            args = arg_handling()
            self.assertEqual(args.duration, "30m")
            self.assertTrue(G.PROFILING_ENABLED)
            self.assertIsNotNone(G.EXECUTION_DEADLINE)
            self.assertIsNotNone(G.STAGE_PROFILER)
            self.assertGreater(G.get_time_remaining(), 1700.0)

    def test_freeze_and_unfreeze_system_memory(self) -> None:
        """Verify freeze_system_memory moves tracked objects into permanent GC generation and unfreeze restores them."""
        import gc
        from core.globalstuff import freeze_system_memory, unfreeze_system_memory

        if not hasattr(gc, "freeze"):
            self.skipTest("gc.freeze is not available on this Python runtime")

        # Create sample container objects
        sample_data = [{"file": f"file_{i}.c", "symbols": (i, i * 2)} for i in range(100)]
        freeze_system_memory()
        frozen_count = gc.get_freeze_count()
        self.assertGreater(frozen_count, 0)

        # Subsequent collections must succeed and not crash
        collected = gc.collect()
        self.assertIsInstance(collected, int)

        # Unfreeze memory
        unfreeze_system_memory()
        self.assertEqual(gc.get_freeze_count(), 0)
        del sample_data


if __name__ == "__main__":
    unittest.main()
