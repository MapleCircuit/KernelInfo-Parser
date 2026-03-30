"""DBHandling.py - MariaDB implementation for DB."""
from globalstuff import G, PointerGetter, SafeDataType, JoinsType, PointerType
import os
import mysql.connector
from typing import Self
from types import TracebackType

MAX_ALLOWED_PACKET = 1073741824


class MariaDB:
    """MariaDB implementation for DB."""

    def __init__(self) -> None:
        """Set default values for DB and start a connection."""
        self.user = "root"
        self.password = "Passe123"  # noqa: S105
        self.db_name = "test"
        self.cnx = self.connect_sql()
        self.cursor = self.cnx.cursor()
        self.cursor.execute("SET SESSION sql_mode = 'NO_AUTO_VALUE_ON_ZERO';")
        self.cursor.execute(f"SET GLOBAL max_allowed_packet = {MAX_ALLOWED_PACKET};")
        self.cnx.commit()

    def __enter__(self) -> Self:
        """Allow use of 'with G.DB() as db:'."""
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception_value: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        """Exit from 'with G.DB() as db:'."""
        return

    def __del__(self) -> None:
        """Safe deletion."""
        if self.cnx.is_connected:
            self.cursor.close()
            self.cnx.close()
        return

    def connect_sql(self) -> object:
        """Connect to sql."""
        if (
            os.path.exists("/.dockerenv")  # noqa: PTH110
            or "docker" in open("/proc/self/cgroup").read()  # noqa: PTH123, SIM115
        ):
            mysql_host = "host.docker.internal"
        else:
            mysql_host = "localhost"
        # Connect to DB
        return mysql.connector.connect(
            host=mysql_host,
            user=self.user,
            password=self.password,
            database=self.db_name,
        )


##################################################################################
    def test_tables(self, tables: object|list[object]|tuple[object]) -> list[object]|None:
        """Test the existance of ALL tables."""
        if not isinstance(tables, (tuple, list)):
            tables = (tables,)

        self.check_if_connected()
        self.cursor.execute("SHOW TABLES")

        existing_tables = self.cursor.fetchall()

        missing_table = [table.table_name for table in tables if table.table_name not in existing_tables]

        if missing_table:
            print(f"We are missing these table: {missing_table}")


        return None

    def drop_table(self, tables: object|list[object]|tuple[object]) -> None:
        """Drop the table that are given, tuple/list accepted."""
        if not isinstance(tables, (tuple, list)):
            tables = (tables,)

        self.check_if_connected()
        self.cursor.execute("SET FOREIGN_KEY_CHECKS = 0;")
        self.cnx.commit()

        sql_drop = "DROP TABLE "
        sql_drop += ", ".join(f"`{x.table_name}`" for x in tables)

        try:
            self.cursor.execute(sql_drop)
            self.cnx.commit()
        except Exception as e:  # noqa: BLE001
            print(f"drop failed : exception : {e}")

        return

    def create_table(self, tables: object|list[object]|tuple[object]) -> None:
        """Create table in DB."""
        if not isinstance(tables, (tuple, list)):
            tables = (tables,)

        self.check_if_connected()

        for table in tables:
            sql_table = f"CREATE TABLE {table.table_name} "
            sql_table += f"({', '.join(map(' '.join, table.init_columns))} "
            sql_table += f", PRIMARY KEY ({', '.join(table.init_primary)}) "
            if table.init_foreign:
                sql_table += " ".join(
                    f",FOREIGN KEY ({x[0]}) REFERENCES {x[1]}({x[2]})" for x in table.init_foreign
                )
            sql_table += ")"
            if G.OVERRIDE_TABLE_CREATION_PRINT:
                print(f"Created table:{table.table_name}")
                print(sql_table)
            self.check_if_connected()
            self.cursor.execute(sql_table)
            if table.initial_insert:
                self.insert(table, table.initial_insert)

        self.cnx.commit()
        return

    def get_next_id(self, table: object) -> int:
        """Get next id/primary key for given table."""
        self.check_if_connected()

        self.cursor.execute(
            f"SELECT COALESCE(MAX({table.init_columns[0][0]}), 0)+1 FROM {table.table_name};",  # noqa: S608
        )

        return self.cursor.fetchone()[0]

    def check_if_connected(self) -> None:
        """If not connected, reconnect."""
        for attempt in range(3):
            if self.cnx.is_connected:
                break
            print(f"No SQL connection attempt:{attempt}")
            self.cnx = self.connect_sql()
            self.cursor = self.cnx.cursor()
            self.cursor.execute("SET SESSION sql_mode = 'NO_AUTO_VALUE_ON_ZERO';")
            self.cursor.execute(
                f"SET GLOBAL max_allowed_packet = {MAX_ALLOWED_PACKET};",
            )
            self.cnx.commit()

    def insert(self, table: object, data: tuple[tuple[SafeDataType, ...], ...]) -> None:
        """Insert set(s) of data in DB table."""
        self.check_if_connected()

        sql = f"INSERT INTO {table.table_name} VALUES "  # noqa: S608
        sql += f"({','.join(('%s',) * table.length)})"
        if isinstance(data[0], (tuple, list)):
            if len(data) > (MAX_ALLOWED_PACKET // 512):
                temp_len = len(data)
                x = 0
                while x < temp_len:
                    if (x + (MAX_ALLOWED_PACKET // 512)) > temp_len:
                        self.cursor.executemany(sql, data[x:])
                    else:
                        self.cursor.executemany(
                            sql, data[x : (x + (MAX_ALLOWED_PACKET // 512))],
                        )
                    x += MAX_ALLOWED_PACKET // 512
                self.cnx.commit()
                return

            self.cursor.executemany(sql, data)
            self.cnx.commit()
            return

        self.cursor.execute(sql, data)
        self.cnx.commit()
        return

    def update(self, table: object, data: tuple[tuple[SafeDataType, ...], ...]) -> None:
        """Update set(s) of data in DB table."""
        self.check_if_connected()

        sql = f"INSERT INTO {table.table_name} "
        sql += f"({', '.join(column[0] for column in table.init_columns)}) VALUES "
        sql += f"({','.join(('%s',) * table.length)}) ON DUPLICATE KEY UPDATE "
        updatable_columns = []
        for x, column in enumerate(table.init_columns):
            if x not in table.primary:
                updatable_columns.append(f"{column[0]} = VALUES({column[0]})")
        sql += ", ".join(updatable_columns)

        self.cursor.executemany(sql, data)
        self.cnx.commit()
        return

    def select(self,table: object, data: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType]:
        """Select based on not None data, using DB table."""
        self.check_if_connected()

        sql = f"SELECT * FROM {table.table_name} WHERE "  # noqa: S608

        where_clauses = []

        for x, val in enumerate(data):
            if val is not None:
                where_clauses.append(f"{table.init_columns[x][0]}=%s")

        sql += " AND ".join(where_clauses)
        sql += " LIMIT 1"
        self.cursor.execute(sql, tuple(filter(lambda val: val is not None, data)))

        return self.cursor.fetchone()

    def view_select(
        self,
        tables: list[object] | tuple[object],
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType]:
        """View Select based on not None data and joins, using DB tables."""
        initial_pointer = PointerGetter(joins).get_first_pointer()

        sql = f"SELECT * FROM {tables[initial_pointer[0]].table_name} AS A1"  # noqa: S608

        data_offset = 0
        where_clauses = []
        for i, init_column in enumerate(tables[initial_pointer[0]].init_columns):
            if columns[i] is not None:
                where_clauses.append(f" A1.{init_column[0]}=%s")
            data_offset += 1

        # Execute single table joins
        if len(joins[0]) == 1:
            sql += " WHERE "
            sql += " AND".join(where_clauses)
            sql += " LIMIT 1"
            self.cursor.execute(sql, tuple(filter(lambda val:val is not None, columns)))
            return self.cursor.fetchone()

        table_id_to_alias_dict = {initial_pointer[0]: 1}
        alias_offset = 1

        for join in joins:
            for x in range(join[2]):
                alias_offset += 1
                if x == 0:
                    table_id_to_alias_dict[join[1][0]] = alias_offset
                sql += f" JOIN {tables[join[1][0]].table_name} A{alias_offset} ON A{table_id_to_alias_dict[join[0][0]]}.{tables[join[0][0]].init_columns[join[0][1]][0]} = A{alias_offset}.{tables[join[1][0]].init_columns[join[1][1]][0]}"

                for init_column in tables[join[1][0]].init_columns:
                    if columns[data_offset] is not None:
                        where_clauses.append(f" A{alias_offset}.{init_column[0]}=%s")
                    data_offset += 1

        sql += " WHERE "
        sql += " AND".join(where_clauses)
        sql += " LIMIT 1"
        self.cursor.execute(sql, tuple(filter(lambda val: val is not None, columns)))

        return self.cursor.fetchone()

    def view_select_multiple(
        self,
        tables: list[object] | tuple[object],
        joins: JoinsType,
        columns: tuple[SafeDataType, ...],
    ) -> tuple[SafeDataType]:
        """View Select based on not None data and joins, using DB tables."""
        initial_pointer = PointerGetter(joins).get_first_pointer()

        sql = f"SELECT * FROM {tables[initial_pointer[0]].table_name} AS A1"  # noqa: S608

        data_offset = 0
        where_clauses = []
        for i, init_column in enumerate(tables[initial_pointer[0]].init_columns):
            if columns[i] is not None:
                where_clauses.append(f" A1.{init_column[0]}=%s")
            data_offset += 1

        # Execute single table joins
        if len(joins[0]) == 1:
            sql += " WHERE"
            sql += " AND".join(where_clauses)
            print(f"sql={sql}")
            print(f"columns={columns}")
            self.cursor.execute(sql, tuple(filter(lambda val:val is not None, columns)))
            return self.cursor.fetchall()

        table_id_to_alias_dict = {initial_pointer[0]: 1}
        alias_offset = 1

        for join in joins:
            for x in range(join[2]):
                alias_offset += 1
                if x == 0:
                    table_id_to_alias_dict[join[1][0]] = alias_offset
                sql += f" JOIN {tables[join[1][0]].table_name} A{alias_offset} ON A{table_id_to_alias_dict[join[0][0]]}.{tables[join[0][0]].init_columns[join[0][1]][0]} = A{alias_offset}.{tables[join[1][0]].init_columns[join[1][1]][0]}"

                for init_column in tables[join[1][0]].init_columns:
                    if columns[data_offset] is not None:
                        where_clauses.append(f" A{alias_offset}.{init_column[0]}=%s")
                    data_offset += 1

        sql += " WHERE"
        sql += " AND".join(where_clauses)
        self.cursor.execute(sql, tuple(filter(lambda val: val is not None, columns)))

        return self.cursor.fetchall()

    def create_index(self, index_name: str, table: object, rows: tuple[PointerType],
        ) -> None:
        """Create an index in the DB."""
        sql = f"CREATE INDEX {index_name} ON {table.table_name} "
        sql += f"({', '.join(table.init_columns[x[1]][0] for x in rows)})"
        self.cursor.execute(sql)
        self.cnx.commit()
        return

    def remove_index(self, index_name: str, table: object,
        ) -> None:
        """Remove an index in the DB."""
        sql = f"ALTER TABLE {table.table_name} DROP INDEX {index_name}"
        self.cursor.execute(sql)
        self.cnx.commit()
        return
