"""db_engine/base.py - Base Abstract Database Engine Interface Contract.

Defines the core database backend API expected across KernelInfo-Parser,
Table Engine (TEDirectDB), and ChangeSet execution.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Sequence, Self
from types import TracebackType
from globalstuff import SafeDataType, JoinsType, PointerType

if TYPE_CHECKING:
    from TableHandling import Table


class BaseDBEngine(ABC):
    """Abstract Base Class for database engine drivers."""

    @abstractmethod
    def __init__(self) -> None:
        """Initialize database engine parameters and session."""
        pass

    @abstractmethod
    def __enter__(self) -> Self:
        """Context manager entry."""
        pass

    @abstractmethod
    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception_value: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        """Context manager exit."""
        pass

    @abstractmethod
    def close(self) -> None:
        """Safely release connection and cursor resources."""
        pass

    @abstractmethod
    def get_next_id(self, table: Table) -> int:
        """Query next sequence integer ID for target table."""
        pass

    @abstractmethod
    def select(self, table: Table, data: tuple[SafeDataType, ...]) -> tuple[SafeDataType, ...] | None:
        """Single-row select with wildcard (None) column filtering."""
        pass

    @abstractmethod
    def view_select(
        self,
        tables: Sequence[Table] | dict[int, Table],
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType, ...] | None:
        """Joined single-row select with wildcard column filtering."""
        pass

    @abstractmethod
    def view_select_multiple(
        self,
        tables: Sequence[Table] | dict[int, Table],
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> list[tuple[SafeDataType, ...]]:
        """Joined multi-row select with wildcard column filtering."""
        pass

    @abstractmethod
    def insert(
        self,
        table: Table,
        data: tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...],
    ) -> None:
        """Batch insert rows matching table schema."""
        pass

    @abstractmethod
    def update(
        self,
        table: Table,
        data: tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...],
    ) -> None:
        """Batch upsert/update rows."""
        pass

    @abstractmethod
    def create_table(self, tables: Sequence[Table] | Table) -> None:
        """Create database tables from Table schema specifications."""
        pass

    @abstractmethod
    def drop_table(self, tables: Sequence[Table] | Table) -> None:
        """Drop database tables."""
        pass

    @abstractmethod
    def create_index(self, index_name: str, table: Table, rows: tuple[PointerType, ...]) -> None:
        """Create composite or single-column index on target table."""
        pass

    @abstractmethod
    def remove_index(self, index_name: str, table: Table) -> None:
        """Remove index from target table."""
        pass

    @abstractmethod
    def test_tables(self, tables: Sequence[Table] | Table) -> list[str] | None:
        """Check for existence of registered tables in catalog."""
        pass
