"""Index management and relational-integrity checks of the MariaDB driver."""
from __future__ import annotations

from typing import TYPE_CHECKING, Sequence
import mysql.connector
from core.globalstuff import (
    PointerType,
)


if TYPE_CHECKING:
    from core.TableHandling import Table



class MariaDBIndexes:
    """Index management and relational-integrity checks of the MariaDB driver."""

    def index_exists(self, index_name: str, table: Table) -> bool:
        """Check if an index exists on the specified table."""
        self.check_if_connected()
        try:
            self._cur.execute(f"SHOW INDEX FROM {table.table_name} WHERE Key_name = %s", (index_name,))
            return bool(self._cur.fetchall())
        except Exception:
            return False

    def create_index(
        self,
        index_name: str,
        table: Table,
        rows: tuple[PointerType, ...],
    ) -> None:
        """Create database index on target table columns if not already existing.

        Args:
            index_name: Name identifier for index.
            table: Target Table schema instance.
            rows: Tuple of pointer tuples `(table_id, col_idx)`.

        Process:
            1. Checks if index already exists via `SHOW INDEX`.
            2. If not, executes `CREATE INDEX index_name ON table (cols)`.
            3. Commits transaction and handles duplicate index exceptions safely.

        Outputs:
            None.
        """
        self.check_if_connected()
        try:
            if self.index_exists(index_name, table):
                return
            sql = f"CREATE INDEX {index_name} ON {table.table_name} "
            sql += f"({', '.join(table.init_columns[x[1]][0] for x in rows)})"
            self._cur.execute(sql)
            self.cnx.commit()
        except mysql.connector.Error as e:
            # 1061: Duplicate key name
            if getattr(e, "errno", None) == 1061 or "Duplicate key name" in str(e):
                return
            raise

    def remove_index(
        self,
        index_name: str,
        table: Table,
    ) -> None:
        """Drop existing index from target table if it exists.

        Args:
            index_name: Name identifier of index to drop.
            table: Target Table schema instance.

        Process:
            1. Checks if index exists via `SHOW INDEX`.
            2. If exists, executes `ALTER TABLE table DROP INDEX index_name`.
            3. Commits transaction and handles missing index exceptions safely.

        Outputs:
            None.
        """
        self.check_if_connected()
        try:
            if not self.index_exists(index_name, table):
                return
            sql = f"ALTER TABLE {table.table_name} DROP INDEX {index_name}"
            self._cur.execute(sql)
            self.cnx.commit()
        except mysql.connector.Error as e:
            # 1091: Can't DROP 'index'; check that column/key exists
            if getattr(e, "errno", None) == 1091 or "check that column/key exists" in str(e):
                return
            raise

    def create_indexes(
        self,
        indexes: Sequence[tuple[str, Table, tuple[PointerType, ...]]],
        max_workers: int | None = None,
    ) -> None:
        """Create multiple database indexes in parallel across dedicated worker connections.

        Args:
            indexes: Sequence of index specifications `(index_name, table, rows)`.
            max_workers: Optional max concurrency limit (default 8).

        Outputs:
            None.
        """
        if not indexes:
            return

        if len(indexes) == 1:
            idx_name, tbl, rows = indexes[0]
            self.create_index(idx_name, tbl, rows)
            return

        from concurrent.futures import ThreadPoolExecutor
        workers = min(len(indexes), max_workers or 8)

        def _worker_create(item: tuple[str, Table, tuple[PointerType, ...]]) -> None:
            idx_name, tbl, rows = item
            worker_db = self.__class__()
            try:
                worker_db.create_index(idx_name, tbl, rows)
            finally:
                worker_db.close()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(_worker_create, indexes))

    def remove_indexes(
        self,
        indexes: Sequence[tuple[str, Table]],
        max_workers: int | None = None,
    ) -> None:
        """Remove multiple database indexes in parallel across dedicated worker connections.

        Args:
            indexes: Sequence of index specifications `(index_name, table)`.
            max_workers: Optional max concurrency limit (default 8).

        Outputs:
            None.
        """
        if not indexes:
            return

        if len(indexes) == 1:
            idx_name, tbl = indexes[0]
            self.remove_index(idx_name, tbl)
            return

        from concurrent.futures import ThreadPoolExecutor
        workers = min(len(indexes), max_workers or 8)

        def _worker_remove(item: tuple[str, Table]) -> None:
            idx_name, tbl = item
            worker_db = self.__class__()
            try:
                worker_db.remove_index(idx_name, tbl)
            finally:
                worker_db.close()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(_worker_remove, indexes))

    def verify_relational_integrity(self, tables: Sequence[Table]) -> dict[str, int]:
        """Verify relational foreign key integrity across tables and return any orphan record counts.

        Args:
            tables (Sequence[Table]): Sequence of registered schema tables.

        Process:
            Iterates through registered tables with foreign key constraints, executing SQL anti-joins
            to check for orphaned foreign key references.

        Outputs:
            dict[str, int]: Mapping of constraint description to orphan count (empty if valid).
        """
        self.check_if_connected()
        violations: dict[str, int] = {}
        for tbl in tables:
            if not getattr(tbl, "init_foreign", None):
                continue
            for fk in tbl.init_foreign:
                local_col, foreign_tbl, foreign_col = fk
                query = (
                    f"SELECT COUNT(*) FROM `{tbl.table_name}` child "
                    f"LEFT JOIN `{foreign_tbl}` parent ON child.`{local_col}` = parent.`{foreign_col}` "
                    f"WHERE child.`{local_col}` IS NOT NULL AND parent.`{foreign_col}` IS NULL;"
                )
                try:
                    self._cur.execute(query)
                    res = self._cur.fetchone()
                    count = res[0] if res else 0
                    if count > 0:
                        violations[f"{tbl.table_name}.{local_col} -> {foreign_tbl}.{foreign_col}"] = count
                except mysql.connector.Error:
                    pass
        return violations
