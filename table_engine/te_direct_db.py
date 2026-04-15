"""table_engine/te_direct_db.py - Unoptimized TE that ask the DB for everything."""  # noqa: INP001
from globalstuff import SafeDataType, JoinsType, PointerGetter
from operator import itemgetter


# Table Engine
class TEDirectDB:
    """Unoptimized TE that ask the DB for everything."""

    def __init__(self) -> None:
        """Initialize the variables needed for TE, you still need to do TE.start()."""
        self.tables = {}
        self.queued_set = {}
        self.queued_update = {}
        self.queued_view = {}
        self.next_id = {}
        self.db = None

    def __del__(self) -> None:  # noqa: D105
        del self.queued_set
        del self.queued_update
        del self.db
        return

    def start_new_db(self, db: classmethod) -> None:
        """Start/restart a DB for TE use only."""
        del self.db
        self.db = db()
        return

    def start(self, tables: list, db: classmethod) -> None:
        """Initialize all tables."""
        if not isinstance(tables, (tuple, list)):
            tables = (tables,)

        self.db = db()

        self.queued_view = {}

        for table in tables:
            self.tables[table.table_id] = table
            self.queued_set[table.table_id] = {}
            self.queued_update[table.table_id] = []
            self.next_id[table.table_id] = self.db.get_next_id(table)

        return

    def get(self, table_id: int, columns: tuple[SafeDataType],
    ) -> tuple[SafeDataType] | None:
        """Pass select instructions to DB."""
        if self.tables[table_id].initial_insert is None:
            init_length = 1
        else:
            init_length = len(self.tables[table_id].initial_insert)+1
        if self.next_id[table_id] <= init_length:
            return None
        return self.db.select(self.tables[table_id], columns)

    def set(self, table_id: int, columns: tuple[SafeDataType],
    ) -> tuple[SafeDataType]:
        """Process set instructions with local, will check if no_dup, for dup."""
        if self.tables[table_id].no_duplicate:
            current_set = self.queued_set[table_id].get(columns[1:])
            if current_set:
                return (current_set, *columns[1:])

            self.queued_set[table_id][columns[1:]] = self.next_id[table_id]
            self.next_id[table_id] += 1

            return (self.next_id[table_id] - 1, *columns[1:])

        if columns[0] is None:
            self.queued_set[table_id][self.next_id[table_id]] = (
                self.next_id[table_id],
                *columns[1:],
            )
            self.next_id[table_id] += 1
            return self.queued_set[table_id][self.next_id[table_id] - 1]

        self.queued_set[table_id][
            itemgetter(*self.tables[table_id].primary)(columns)
        ] = columns

        return columns

    def view_get(self, joins: JoinsType, columns: tuple[SafeDataType],
    ) -> tuple[SafeDataType] | None:
        """Pass view_select instructions to DB."""
        initial_table_id = PointerGetter(joins).get_first_table_id()
        if self.next_id[initial_table_id] <= len(self.tables[initial_table_id].initial_insert)+1:
            return None
        return self.db.view_select(self.tables, joins, columns)

    def view_get_multiple(self, joins: JoinsType, columns: tuple[SafeDataType],
    ) -> tuple[SafeDataType] | None:
        """Pass view_select_multiple instructions to DB."""
        return self.db.view_select_multiple(self.tables, joins, columns)


    def view_set(self, joins: JoinsType, columns: tuple[SafeDataType],
    ) -> tuple[SafeDataType]:
        """Process view_set with local, will divide into set(s), will check for dup."""
        filtered_columns = tuple(filter(lambda val: val is not None, columns))

        try:
            current_view_id = self.queued_view[joins].get(filtered_columns)
            if current_view_id is not None:
                return tuple(val if val is not None else current_view_id for val in columns)

        except KeyError:
            current_view_id = None

        if self.queued_view.get(joins) is None:
            self.queued_view[joins] = {}

        main_table_id = PointerGetter(joins).get_first_table_id()
        current_view_id = self.next_id[main_table_id]

        self.queued_view[joins][filtered_columns] = current_view_id
        self.next_id[main_table_id] += 1

        result = tuple(
            (val if val is not None else current_view_id
            )for val in columns
        )

        data_offset = 0
        for repeat, pointer in PointerGetter(joins):
            for _x in range(repeat):
                t_len = self.tables[pointer[0]].length
                self.queued_set[pointer[0]][
                    itemgetter(*self.tables[pointer[0]].primary)(result)
                ] = result[data_offset : data_offset + t_len]
                data_offset += t_len

        return result

    def update(self, table_id: int, columns: tuple[SafeDataType],
    ) -> tuple[SafeDataType]:
        """Add to the queued list of update."""
        self.queued_update[table_id].append(columns)
        return columns


    def commit(self, table_id: int) -> None:
        """Commit all change to DB."""
        #print(f"======{self.tables[table_id].table_name}======")
        if self.queued_set[table_id]:
            if self.tables[table_id].no_duplicate:
                self.db.insert(
                    self.tables[table_id],
                    tuple((x[1], *x[0]) for x in self.queued_set[table_id].items()),
                )
            else:
                self.db.insert(
                    self.tables[table_id],
                    tuple(self.queued_set[table_id].values()),
                )

        if self.queued_update[table_id]:
            self.db.update(self.tables[table_id], tuple(self.queued_update[table_id]))
        return
