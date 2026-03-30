"""TableHandling.py - Implements ChangeSet, Table used to interface with TE and DB."""
from globalstuff import (
    G,
    PointerGetter,
    type_check,
    T_C,
    REF_ROOT,
    REF_POS,
    REF_FILE,
    REF_C_AST,
    CONTINUE_EXCEPTION,
    FILE_ERROR,
    REF_NOT_RESOLVABLE,
    OP_DONE,
    OP_SET,
    OP_UPDATE,
    OP_REF,
    OP_VIEW_DONE,
    OP_VIEW_SET,
    LinkType,
    RouteType,
    PointerType,
    JoinsType,
    OperationType,
    SafeDataType,
    UnSafeDataType,
)
import sys
from parser.c_ast import c_ast_parse
from operator import itemgetter
from typing import Self
from types import TracebackType

# Basics in how this Works:
# ***At the top of globalstuff.py we have a list of all the types, it may help.***

# When processing a file, we will generate Operations.
# Operations are either already process or to be processed.
# An Operation will look like this: (m_m_v_main.table_id, OP_SET, (None, "v3.0"))
# Lets define the acceptable values/types in each positions:
# ( Operation
#   m_m_v_main.table_id,    # OP.target => table_id(int) OR joins(JoinsType) *joins defined soon.
#   OP_SET,                 # OP.type   => operation_type(int)
#   (None, "v3.0"),         # OP.data   => data(UnSafeDataType)
# )

# These Operations are stored inside of Change_Set.cs[].
# **If you see CS, it is the initialized form of Change_Set**
# Once an operation is fully processed, the resulting data is stored in Change_Set.cs_result[]

# Data can either be Safe or UnSafe (SafeDataType,UnSafeDataType)
# SafeDataType   =         int|str|None # Data without references.
# UnSafeDataType = RefType|int|str|None # Data with references.

# References (RefType) are encoded inside of OP.data.
# Lets define the acceptable values/types in each positions:
# ( Reference
#   (m_m_v_main.table_id, 0),   # REF.query => query(PointerType)
#   OP_REF,                     # REF.type  => reference_type(int)
#   (REF_ROOT, REF_OLD),        # REF.route  => route(RouteType)   LinkType
# )

# PointerType is a tuple containing a table_id and column_id.
# This is the main way we will be targeting a specific table/column.

# Route is used to store and recover data in a standardized way.
# A Route contains Links which allows us to know where the data should be stored
# for it to be found again. Here is an example of how we could store and retrieve data:
# **It is assumed that we start the route in REF_ROOT.**
#   with CS(REF_OLD):
#       # Store m_file_name in (REF_ROOT, REF_OLD)
#       CS.store(m_file_name.get_set(None, CS.current_path))
# *** Later in the code ***
#       CS.store(m_bridge_file.get(
#           gp.Old_VID,
#           # Reference m_file_name.fnid in (REF_ROOT, REF_OLD)
#           # **we are still inside CS(REF_OLD):**
#           CS.ref(m_file_name.fnid),
#           None,
#       ))
#
# CS.store() can also take Links as arguments. Example:
# CS.store(...., REF_ROOT, REF_OLD)
#
# When used, the links within the Route are Resolved.
# For example: (REF_ROOT, REF_C_AST, REF_ROOT)
# Once resolve will become: (REF_ROOT,)
#
# Here is the link of all possible Links. Higher or equal will remove previous instances.
#   REF_FILE            => Set the file we are referencing. Is followed by a Path.
#   REF_POS             => Set the position within CS.cs. Is followed by an int.
#   REF_ROOT, REF_C_AST => Set the system the data is a part of.
#   REF_OLD             => Indicate that the data is part of the previous version

# REF_C_AST is able to store multiple items at the same position.


# It is sometime necessary for us to ensure uniqueness across multiple table of our DB.
# For example, m_ast* tables are not individualy unique but unique across all of them.
# We solve this by using View.
#   CS.store(m_ast.view(
#       ((m_ast.ast_id,),),     # Joins
#       None, name, type_id   # Data
#   ))
#
# Joins allows us to set 'INNER JOIN' and exist in 2 form:
# Single table:
#   ((m_ast.ast_id,),), # Only need a Pointer within 2 tuples.
# Multi table:
#   (
#       (m_ast.ast_id, m_ast_include.ast_id, 1),
#       (m_ast.ast_id, m_ast_container.ast_id, 2),
#   )
# In this form the first 2 pointer acts as the 'INNER JOIN'.
# The 3rd argument is the number of repetition of the data. Here is how the data would look:
# (
#   m_ast[0], m_ast[1], m_ast[2],
#   m_ast_include[0], m_ast_include[1],
#   m_ast_container[0], m_ast_container[1], m_ast_container[2], m_ast_container[3], m_ast_container[4], # x1
#   m_ast_container[0], m_ast_container[1], m_ast_container[2], m_ast_container[3], m_ast_container[4], # x2
# )



def is_data_unsafe(data: tuple) -> bool:
    """Check if data contain Refs."""
    return any(type(col) is tuple for col in data)


class ChangeSet:
    """Contain the change and operation of a file."""

    @G.type_check(Self, (str, None),(str, None),(str, None))
    def __init__(  # noqa: D107
        self,
        operation: str | None = None,
        current_path: str | None = None,
        old_path: str | None = None,
    ) -> None:

        if current_path is None and operation is not None:
            cut_file = operation.split("\t")
            self.file_operation = cut_file[0]
            if len(cut_file) == 2:  # noqa: PLR2004
                current_path = cut_file[1]
            else:
                old_path = cut_file[1]
                current_path = cut_file[2]
        else:
            self.file_operation = operation
        self.current_path = current_path
        self.old_path = old_path
        self.cs = []
        self.cs_processed = False
        self.cs_result = []
        self.file = None
        self.store_dict = {}
        self.gp = None
        self.mf = None
        self.route = [REF_ROOT]

    @G.type_check(Self, LinkType)
    def __call__(self, link: LinkType) -> Self:
        """Add link to route."""
        self.route.append(link)
        return self

    def __enter__(self) -> Self:
        """To be used with __call__ to enter route."""
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception_value: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        """Pop link from current route."""
        self.route.pop()
        if len(self.route) == 0:
            self.route = [REF_ROOT]
        return

    def last_not_none(self) -> None:
        """Check if last value in CS.cs is None."""
        if self.cs[-1] is None:
            print("Last not None Error")
            raise CONTINUE_EXCEPTION
        return

    @G.type_check(Self, {UnSafeDataType})
    def resolve_ref_from_tuple(self, *data: UnSafeDataType) -> tuple[SafeDataType, ...]:
        """Resolve all refs from data or raise REF_NOT_RESOLVABLE."""
        output_data = []
        for val in data:
            if type(val) is tuple:
                output_data.append(self.resolve_ref(val[0], val[2]))
                if output_data[-1] is None:
                    raise REF_NOT_RESOLVABLE
            else:
                output_data.append(val)
        return tuple(output_data)

    def execute(self) -> bool:
        """Execute stored operations in CS.cs.

        If the operation cannot be processed,
        the REF_NOT_RESOLVABLE exception will be raised.
        """
        if self.cs_processed:
            return True

        operation_offset = len(self.cs_result)
        for operation in self.cs[operation_offset:]:
            # check if tuple or None
            #if type(operation[2]) is not tuple:
            #    continue

            data = self.resolve_ref_from_tuple(*operation[2])
            op_type = operation[1]

            if op_type in (OP_DONE, OP_VIEW_DONE):
                self.cs_result.append(data)
                continue

            if op_type == OP_SET:
                self.cs_result.append(G.TE.set(operation[0], data))
                continue
            if op_type == OP_UPDATE:
                self.cs_result.append(G.TE.update(operation[0], data))
                continue
            if op_type == OP_VIEW_SET:
                self.cs_result.append(G.TE.view_set(operation[0], data))
                continue

            print(f"ERROR, UNKNOWN OPERATION{operation}")

        self.cs_processed = True
        return True

    @G.type_check(Self, OperationType, {LinkType})
    def store(self, operation: OperationType, *route: LinkType) -> None:
        """Store the operation in CS.cs.

        Will handle route if CS.file_operation is not None.
        """
        if self.file_operation is None:
            self.cs.append(operation)
            return

        parsed_route = tuple(self.route_parse(self.route + list(route)))

        if self.store_dict.get(parsed_route) is None:
            self.store_dict[parsed_route] = {}

        # check for table_id/first join in route
        table_id_pointer = PointerGetter(operation[0]).get_first_pointer()

        ### we should have a set with REF_C_AST in it and we should do if parsed_route[0] in SetThatContainsREF_C_AST:
        if parsed_route[0] == REF_C_AST:
            if self.store_dict[parsed_route].get(table_id_pointer) is None:
                self.store_dict[parsed_route][table_id_pointer] = []

            self.store_dict[parsed_route][table_id_pointer].append(len(self.cs))
            self.cs.append(operation)
            return

        self.store_dict[parsed_route][table_id_pointer] = len(self.cs)
        self.cs.append(operation)
        return


    @G.type_check(Self, RouteType)
    def route_parse(self, route: RouteType) -> list:
        """Parse route to reduce to their minimal usefull form."""
        parsed_route = []

        # REF_FILE, REF_POS,  REF_ROOT    , REF_OLD
        #                    REF_C_AST

        data_bypass = False
        has_ref_file = False
        has_ref_pos = False

        for link in route:
            if data_bypass:
                data_bypass = False
                parsed_route.append(link)
                continue

            if link == REF_FILE:
                has_ref_file = True
                data_bypass = True
                parsed_route = []

            if link == REF_POS:
                has_ref_pos = True
                data_bypass = True
                if not has_ref_file:
                    parsed_route = []

            if link in (REF_ROOT, REF_C_AST) and not has_ref_file and not has_ref_pos:
                parsed_route = []

            parsed_route.append(link)

        return parsed_route

    @G.type_check(Self, PointerType, int)
    def get_value_at(self, query: PointerType, pos: int) -> SafeDataType:
        """Get value at pos in CS.cs, may return None."""
        operation = self.cs[pos]

        if operation[1] in (OP_VIEW_DONE, OP_VIEW_SET):
            joins = operation[0]
            # view_data will return None instead of refs if not processed
            if len(self.cs_result) > pos:
                view_data = self.cs_result[pos]
            else:
                view_data = tuple(
                    None if type(data) is tuple else data for data in operation[2]
                )

            data_offset = 0
            for repeat, pointer in PointerGetter(joins):
                for _x in range(repeat):
                    if pointer[0] == query[0]:
                        return view_data[data_offset + query[1]]
                    data_offset += self.gp.Table_Array[pointer[0]].length
            print("something wrong with get_value_at while in a view")
            print(f"query:{query} pos:{pos}")
            G.emergency_shutdown(456)

        # sanity check the table_id of the target
        if operation[0] != query[0]:
            print("something wrong with get_value_at, table_id do not match")
            print(f"query:{query} pos:{pos}")
            G.emergency_shutdown(457)

        if len(self.cs_result) > pos:
            return self.cs_result[pos][query[1]]

        #result= <   data   >|<  row  >
        result = operation[2][query[1]]
        return None if type(result) is tuple else result

        print("how did we get here")
        return None

    @G.type_check(Self, PointerType, RouteType)
    def resolve_ref(self, query: PointerType, parsed_route: RouteType) -> SafeDataType:
        """Resolve ref, will return None if Nothing/another ref found."""
        data_bypass = False

        for i, route in enumerate(parsed_route):
            if data_bypass:
                data_bypass = False
                continue

            if route == REF_FILE:
                foreign_cs = self.gp.ChangeSet_Dict.get(parsed_route[i + 1])
                if foreign_cs is None:
                    return None

                return foreign_cs.resolve_ref(query, parsed_route[i + 2 :])

            if route == REF_POS:
                return self.get_value_at(query, parsed_route[i + 1])

        stored_route = self.store_dict.get(tuple(parsed_route))
        if stored_route is not None:
            stored_pos = stored_route.get(PointerGetter(query[0]).get_first_pointer())
            if stored_pos is not None:
                if type(stored_pos) is list:
                    return self.get_value_at(query, stored_pos[0])
                return self.get_value_at(query, stored_pos)
        return None

    @G.type_check(Self, PointerType, {LinkType})
    def ref(self, query: PointerType, *route_args: LinkType) -> UnSafeDataType:
        """Create a reference based on the current route + args."""
        if route_args:
            parsed_route = tuple(self.route_parse(self.route + list(route_args)))
        else:
            parsed_route = tuple(self.route_parse(self.route))

        result = self.resolve_ref(query, parsed_route)
        if result is not None:
            return result

        return (query, OP_REF, parsed_route)

    # Will select the right parser and execute it
    def parse(self) -> None:
        """Select the parse based on file type."""
        try:
            current_type = type_check(self.current_path)
            if current_type == T_C:
                c_ast_parse(self)
            else:
                pass
        except FILE_ERROR as e:
            print(f"FILE_ERROR for '{self.file_operation}'={self.current_path}")
            print(e)
            self.cs = []

        return

    def clear_bloat(self) -> None:
        """Remove gp and mf as they could break during pickling."""
        self.gp = None
        self.mf = None
        return

    def __str__(self) -> str:
        """Will print CS.file_operation, CS.cs and CS.cs_result."""
        result = f"CS:file({self.current_path}),op({self.file_operation}),"
        result += f"cs({','.join(map(str, self.cs))}),"
        result += f"cs_result({','.join(map(str, self.cs_result))})"
        return result


class Table:
    """Generate binding for us to manage our table."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        table_id: int,
        table_name: str,
        columns: tuple[tuple[str, ...], ...],
        primary: tuple[str,...],
        foreign: tuple[tuple[str,str,str], ...]|None=None,
        initial_insert: tuple[tuple[SafeDataType, ...], ...]|None=None,
        no_duplicate: bool=False,
        select_procedure: bool|str=False,
    ) -> None:
        """Set columns and create Pointer bindings."""
        # ID in gp.Table_array
        self.table_id = table_id
        # Name of table as a "String"
        self.table_name = table_name
        # Columns with arguments in a tuple:
        # (("col1", "INT", "NOT NULL", "AUTO_INCREMENT"),("col2", "INT", "NOT NULL", "AUTO_INCREMENT"))
        self.init_columns = columns
        self.length = len(columns)
        # Name of the primary key as a "String"
        self.init_primary = primary
        temp_primary = []
        for prim in self.init_primary:
            for x, column in enumerate(self.init_columns):
                if column[0] == prim:
                    temp_primary.append(x)
                    break
        # Tuple of the pos for the primary key, to be used with ittemgetter
        self.primary = tuple(temp_primary)
        # Foreign key in a tuple where:
        # ("Key_name_in_current_table", "Foreign_table_name", "Foreign_key_name")
        self.init_foreign = foreign
        # Initial insert in the form of a tuple with the values to add
        # (0,"this is a value")
        self.initial_insert = initial_insert
        # Will check for already existing values in the current change going into the table
        # before completing a set (post multicore), if one is found, it will return the existing one.
        self.no_duplicate = no_duplicate
        # Tells us how we select the table before processing:
        # False: Will not keep a local copy, get calls will always fail
        # True: Will keep the whole table
        # lambda x: "select .....": Will run the select in order to get the local copy
        # where x = globals()
        self.select_procedure = select_procedure

        # Pointer/query
        for x, column in enumerate(self.init_columns):
            setattr(self, column[0], (self.table_id, x))

        # FIX FOR C_AST
        setattr(sys.modules["parser.c_ast"], self.table_name, self)

        return

    def start_te(self) -> None:
        """Initiate TE."""
        G.TE.start(self)
        return

    @G.type_check(Self, {UnSafeDataType})
    def set(self, *columns: UnSafeDataType) -> OperationType:
        """Create set operation, will execute get_set if no_dup enabled."""
        if self.no_duplicate:
            return self.get_set(*columns)

        return (self.table_id, OP_SET, columns)

    @G.type_check(Self, {SafeDataType})
    def update(self, *columns: SafeDataType) -> OperationType:
        """Create update operation, will execute get to fill None(s)."""
        if is_data_unsafe(columns):
            print(f"""An {self.table_name}.update was done with unresolved refs,
            This is unexpedted behavior. CRASH""")
            print(columns)
            G.emergency_shutdown(55)

        if None not in columns:
            return (self.table_id, OP_UPDATE, columns)

        primary_values = itemgetter(*self.primary)(columns)

        if type(primary_values) is not tuple:
            primary_values = (primary_values,)

        get_columns = primary_values + (None,) * (len(columns) - len(self.primary))

        get_result = G.TE.get(self.table_id, get_columns)

        columns = tuple(
            x[1] if x[1] is not None else get_result[x[0]] for x in enumerate(columns)
        )
        return (self.table_id, OP_UPDATE, columns)

    @G.type_check(Self, {SafeDataType})
    def get(self, *columns: SafeDataType) -> OperationType|None:
        """Create get operation, can return None if nothing found."""
        if is_data_unsafe(columns):
            print(f"""An {self.table_name}.get was done with unresolved refs,
            This is unexpedted behavior. CRASH""")
            print(columns)
            G.emergency_shutdown(55)

        result = G.TE.get(self.table_id, columns)
        if result is None:
            return None

        return (self.table_id, OP_DONE, result)

    @G.type_check(Self, {UnSafeDataType})
    def get_set(self, *columns: UnSafeDataType) -> OperationType:
        """Create get_set operation, will create set if get return None."""
        if not is_data_unsafe(columns):
            result = G.TE.get(self.table_id, columns)
            if result:
                return (self.table_id, OP_DONE, result)

        return (self.table_id, OP_SET, columns)



    @G.type_check(Self, JoinsType, {UnSafeDataType})
    def view(self, joins: JoinsType, *data: UnSafeDataType) -> OperationType:
        """Create view operation, will create view_set if view_get return None."""
        if not is_data_unsafe(data):
            result = G.TE.view_get(joins, data)
            if result:
                return (joins, OP_VIEW_DONE, result)

        return (joins, OP_VIEW_SET, data)

    @G.type_check(Self, JoinsType, {SafeDataType})
    def view_get(self, joins: JoinsType, *data: SafeDataType) -> OperationType|None:
        """Create view_get operation, can return None."""
        if not is_data_unsafe(data):
            result = G.TE.view_get(joins, data)

            if result:
                return (joins, OP_VIEW_DONE, result)

        return None

    @G.type_check(Self, JoinsType, {UnSafeDataType})
    def view_get_multiple(self, joins: JoinsType, *data: UnSafeDataType) -> tuple[tuple[SafeDataType, ...], ...]|None:
        """USE ONLY TO GET A TUPLE OF RESULTS."""
        if is_data_unsafe(data):
            return None
        return G.TE.view_get_multiple(joins, data)


