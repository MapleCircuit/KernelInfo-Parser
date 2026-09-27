"""unit_test/harness.py - Test Harness with Dependency Management, Dual Error Tracing, and DB Reload.

Provides reusable unit testing infrastructure for KernelInfo-Parser subsystems (DB engines, parsers, webapp).
Enforces:
1. Clear dual-trace error reporting (Feature under test + Python stack trace).
2. Explicit test dependency tracking (marking cascading tests as UNTESTABLE when dependencies fail).
3. Automated database re-initialization and clean isolation using prefix-swapped tables (e.g. 't_*').
"""
from __future__ import annotations

import functools
import sys
import traceback
import unittest
from typing import Any, Callable, Sequence, TypeVar

from core.TableHandling import Table
import core.DBLayout as gp

F = TypeVar("F", bound=Callable[..., Any])


class FeatureRegistry:
    """Tracks feature test execution states, dependencies, and failure contexts."""

    def __init__(self) -> None:
        self.status: dict[str, str] = {}
        self.failure_reasons: dict[str, str] = {}

    def reset(self) -> None:
        """Clear all registered feature statuses."""
        self.status.clear()
        self.failure_reasons.clear()

    def record_pass(self, feature: str) -> None:
        """Record successful verification of a feature."""
        self.status[feature] = "PASSED"

    def record_failure(self, feature: str, reason: str) -> None:
        """Record failure of a feature with its root error reason."""
        self.status[feature] = "FAILED"
        self.failure_reasons[feature] = reason

    def check_dependencies(self, dependencies: Sequence[str] | None) -> tuple[bool, str | None]:
        """Check if all prerequisite features have passed.
        
        Returns:
            (can_run, blocked_reason)
        """
        if not dependencies:
            return True, None
        for dep in dependencies:
            dep_status = self.status.get(dep)
            if dep_status == "FAILED":
                reason = self.failure_reasons.get(dep, "Unknown error")
                return False, f"Prerequisite feature '{dep}' failed ({reason})"
            elif dep_status != "PASSED":
                return False, f"Prerequisite feature '{dep}' was not executed or not passed (status: {dep_status})"
        return True, None


# Global registry instance across test executions
GLOBAL_REGISTRY = FeatureRegistry()


class FeatureTestError(AssertionError):
    """AssertionError enriched with structured feature context and dual traceback."""
    pass


class TestStepContext:
    """Tracks active test sub-step and diagnostic notes for granular error reporting."""
    _current_step: str | None = None
    _current_diagnostic: str | None = None

    @classmethod
    def set(cls, step: str, diagnostic: str | None = None) -> None:
        cls._current_step = step
        cls._current_diagnostic = diagnostic

    @classmethod
    def clear(cls) -> None:
        cls._current_step = None
        cls._current_diagnostic = diagnostic = None

    @classmethod
    def get(cls) -> tuple[str | None, str | None]:
        return cls._current_step, cls._current_diagnostic


class test_step:
    """Context manager to scope a specific sub-operation and attach diagnostic explanations.
    
    Usage:
        with test_step("Verifying initial_insert via select()", diagnostic="Failure may be in select() or create_table()"):
            res = self.db.select(...)
    """

    def __init__(self, step: str, diagnostic: str | None = None) -> None:
        self.step = step
        self.diagnostic = diagnostic

    def __enter__(self) -> test_step:
        TestStepContext.set(self.step, self.diagnostic)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is None:
            TestStepContext.clear()


def test_feature(
    feature_name: str,
    depends_on: Sequence[str] | None = None,
    registry: FeatureRegistry | None = None,
) -> Callable[[F], F]:
    """Decorator for unit tests to enforce feature tracking, dependency blocking, and dual error reporting.
    
    Args:
        feature_name: Logical name of the feature being tested (e.g. 'create_table', 'insert').
        depends_on: Prerequisite feature names that must succeed before this test can run.
        registry: Optional custom registry instance (defaults to GLOBAL_REGISTRY).
    """
    reg = registry if registry is not None else GLOBAL_REGISTRY

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            reg = getattr(self, "feature_registry", None) or registry or GLOBAL_REGISTRY
            prefix = getattr(self, "engine_name", "")
            scoped_feature = f"{prefix}_{feature_name}" if prefix else feature_name
            scoped_deps = [f"{prefix}_{d}" if prefix else d for d in depends_on] if depends_on else None

            # 1. Dependency Precondition Check
            can_run, block_reason = reg.check_dependencies(scoped_deps)
            if not can_run:
                msg = f"UNTESTABLE: Feature '{scoped_feature}' is blocked - {block_reason}"
                reg.record_failure(scoped_feature, f"Blocked by dependency ({block_reason})")
                raise unittest.SkipTest(msg)

            TestStepContext.clear()
            try:
                # 2. Execute Test
                result = func(self, *args, **kwargs)
                reg.record_pass(scoped_feature)
                return result
            except unittest.SkipTest:
                # Allow manual SkipTest to pass through
                raise
            except Exception as exc:
                # 3. Handle Error: Record failure, format dual trace, and trigger DB reload
                full_tb = traceback.format_exc()
                doc = func.__doc__.strip() if func.__doc__ else "No test docstring provided."
                step, diagnostic = TestStepContext.get()

                error_lines = [
                    "",
                    "=" * 80,
                    f"[FEATURE TEST ERROR] Feature: '{scoped_feature}'",
                    f"Test Function   : {func.__module__}.{func.__qualname__}",
                    f"Feature Context : {doc}",
                ]
                if step:
                    error_lines.append(f"Failing Sub-Step: {step}")
                if diagnostic:
                    error_lines.append(f"Step Diagnostic : {diagnostic}")
                error_lines.extend([
                    f"Error Type      : {type(exc).__name__}",
                    f"Error Message   : {exc}",
                    "-" * 80,
                    "Python Traceback:",
                    full_tb.rstrip(),
                    "=" * 80,
                    "",
                ])
                error_msg = "\n".join(error_lines)

                reg.record_failure(scoped_feature, f"{type(exc).__name__}: {exc}")

                # 4. Trigger DB Reload if test case defines reload_test_db()
                if hasattr(self, "reload_test_db") and callable(self.reload_test_db):
                    try:
                        self.reload_test_db()
                    except Exception as reload_err:
                        error_msg += f"\n[WARNING] DB Reload after failure also encountered an error: {reload_err}\n"

                raise FeatureTestError(error_msg) from exc
            finally:
                TestStepContext.clear()

        return wrapper  # type: ignore[return-value]

    return decorator


def clone_table_for_testing(table: Table, prefix: str = "t_") -> Table:
    """Clone a production Table schema definition, swapping table name and foreign keys with a test prefix.
    
    Args:
        table: Original Table object from core.DBLayout.
        prefix: New prefix to apply (defaults to 't_').
        
    Returns:
        New isolated Table instance targeting test prefix.
    """
    old_name = table.table_name
    new_name = prefix + (old_name[2:] if old_name.startswith("m_") else old_name)

    # Rewrite foreign keys target tables
    new_foreign = None
    if table.init_foreign:
        fks = []
        for local_col, ref_table, ref_col in table.init_foreign:
            new_ref = prefix + (ref_table[2:] if ref_table.startswith("m_") else ref_table)
            fks.append((local_col, new_ref, ref_col))
        new_foreign = tuple(fks)

    new_hashing = table.hashing_table
    if isinstance(new_hashing, str):
        new_hashing = prefix + (new_hashing[2:] if new_hashing.startswith("m_") else new_hashing)

    return Table(
        table_id=table.table_id,
        table_name=new_name,
        columns=table.init_columns,
        primary=table.init_primary,
        foreign=new_foreign,
        initial_insert=table.initial_insert,
        no_duplicate=table.no_duplicate,
        te_cached=table.te_cached,
        version_scoped=table.version_scoped,
        hashing_table=new_hashing,
    )


class TestTableCollection:
    """Container holding cloned test tables accessible by name or list."""

    def __init__(self, prefix: str = "t_") -> None:
        self.prefix = prefix
        self.tables_list: list[Table] = [clone_table_for_testing(t, prefix=prefix) for t in gp.TABLES]
        self.tables_by_name: dict[str, Table] = {t.table_name: t for t in self.tables_list}
        self.tables_by_id: dict[int, Table] = {t.table_id: t for t in self.tables_list}

    def __getitem__(self, name_or_id: str | int) -> Table:
        if isinstance(name_or_id, int):
            return self.tables_by_id[name_or_id]
        return self.tables_by_name[name_or_id]

    def __getattr__(self, name: str) -> Table:
        if name in self.tables_by_name:
            return self.tables_by_name[name]
        raise AttributeError(f"No test table named '{name}'")

    def __iter__(self):
        return iter(self.tables_list)

    def __len__(self) -> int:
        return len(self.tables_list)
