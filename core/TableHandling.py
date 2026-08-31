"""TableHandling.py - ChangeSet Management & Relational Table Bindings.

===============================================================================
TABLE HANDLING & CHANGE-SET ARCHITECTURAL GUIDE
===============================================================================
This module implements the core data structures used to record, resolve, and
execute database operations for individual file changes across project versions.

1. HOW OPERATIONS WORK:
-------------------------------------------------------------------------------
When processing a file diff, operations are queued inside `ChangeSet.cs[]`.
An operation is a tuple structured as:
  (target, operation_type, data)

Acceptable values in each position:
  - target: table_id (int) OR joins tuple (JoinsType)
  - operation_type: OP_SET, OP_UPDATE, OP_VIEW_SET, OP_REF_VIEW, OP_DONE, OP_VIEW_DONE
  - data: tuple of row values (SafeDataType or UnSafeDataType)

Examples:
  - Simple Set:   (m_v_main.table_id, OP_SET, (None, "v3.0"))
  - View Set:     (joins_tuple, OP_VIEW_SET, (None, "struct foo", 12))

Data classification:
  - SafeDataType:   int | str | None (Data containing pure primitive values)
  - UnSafeDataType: RefType | int | str | None (Data containing reference tuples)

2. REFERENCES (RefType) & POINTERS (PointerType):
-------------------------------------------------------------------------------
PointerType is a tuple containing `(table_id, col_idx)` generated dynamically
on Table objects (e.g., `m_file_name.fnid` = `(1, 0)`).

A Reference tuple (RefType) is encoded within operation data as:
  (query_pointer, OP_REF, route_tuple)

Example reference tuple:
  ((m_v_main.table_id, 0), OP_REF, (REF_ROOT, REF_OLD))

3. ROUTES & LINK CONTEXTS:
-------------------------------------------------------------------------------
Routes define how operations are indexed in `CS.store_dict` and located.
A Route contains Links allowing data to be stored and retrieved reliably across files
and AST parsing scopes.

Usage with context managers:
  with CS(REF_OLD):
      # Store m_file_name in route (REF_ROOT, REF_OLD)
      CS.store(m_file_name.get_set(None, CS.current_path))
      
      # Reference m_file_name.fnid from (REF_ROOT, REF_OLD)
      CS.store(m_bridge_file.get(
          gp.Old_VID,
          CS.ref(m_file_name.fnid),
          None,
      ))

Available Link Types:
  - REF_FILE: Directs reference lookup to another file's ChangeSet (followed by file path).
  - REF_POS: Directs reference lookup to a specific numerical index in `CS.cs`. 
  - REF_MULTI: Stores multiple items under the same key. `with CS(REF_MULTI):` will create a new key.
  - REF_ROOT / REF_C_AST: Sets system scope context. 
  - REF_OLD: Indicates that the data belongs to the previous project version.

4. VIEWS & RELATIONAL JOINS (JoinsType):
-------------------------------------------------------------------------------
Views ensure uniqueness across multi-table relationships (e.g., `m_ast` join graphs):
  CS.store(m_ast.view(
      ((m_ast.ast_id,),),    # Joins graph tuple
      None, name, type_id   # Data tuple
  ))

Joins format:
  - Single table join: `((m_ast.ast_id,),)`
  - Multi-table join:
    (
        (m_ast.ast_id, m_ast_include.ast_id, 1),
        (m_ast.ast_id, m_ast_container.ast_id, 2),
    )
  The 3rd element specifies the repetition count of joined table columns.

5. DATA SANITIZATION:
-------------------------------------------------------------------------------
All scalar values sent to the Table Engine (`G.TE`) or stored in execution results
are de-subclassed via `to_safe_data()` so that `IntEnum` items (e.g., `ASTT.C_struct`)
are converted into pure native `int` objects to maintain database driver compatibility.
===============================================================================
"""
from __future__ import annotations

import sys
import time
import logging
from operator import itemgetter
from typing import Any, Self
from types import TracebackType
from enum import Enum

from core.globalstuff import (
    G,
    PointerGetter,
    type_check,
    T_C,
    T_ASM,
    T_KCONFIG,
    T_RUST,
    T_MAINTAINERS,
    T_CREDITS,
    T_RAW,
    REF_ROOT,
    REF_POS,
    REF_FILE,
    REF_C_AST,
    REF_MULTI,
    REF_NO_REF,
    CONTINUE_EXCEPTION,
    FILE_ERROR,
    REF_NOT_RESOLVABLE,
    OP_DONE,
    OP_SET,
    OP_UPDATE,
    OP_REF,
    OP_REF_VIEW,
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
from parser.c_ast.c_ast import c_ast_parse
from parser.asm_ast.asm_ast import asm_ast_parse
from parser.kconfig_ast.kconfig_ast import kconfig_ast_parse
from core.Profiler import PipelineProfiler

logger = logging.getLogger(__name__)


_REF_BYPASS_LINKS = frozenset({REF_POS, REF_MULTI})
_REF_RESET_LINKS = frozenset({REF_ROOT, REF_C_AST, REF_NO_REF})


def is_data_unsafe(data: tuple) -> bool:
    """Check if a data tuple contains any unresolved references (RefType).

    Args:
        data: Tuple of column values.

    Returns:
        True if any element in `data` is a tuple (indicating an unresolved reference
        tuple `(query, OP_REF, route)`), False if all elements are safe primitive values.
    """
    for col in data:
        if type(col) is tuple:
            return True
    return False


def to_safe_data(val: Any) -> SafeDataType:
    """Convert a value into strict primitive SafeDataType (pure int, pure str, or None).

    Ensures that IntEnum/Enum subclasses (like ASTT) or custom subclasses
    are converted into pure native Python `int` or `str` objects before being sent
    to the Table Engine or database driver.

    Args:
        val: Any scalar value, enum member, or None.

    Returns:
        Pure primitive `int`, `str`, or `None`.
    """
    t = type(val)
    if t is int or t is str or val is None:
        return val
    if t is bool:
        return int(val)
    if isinstance(val, Enum):
        v = val.value
        return int(v) if type(v) is int else str(v)
    val_attr = getattr(val, "value", None)
    if val_attr is not None and (type(val_attr) is int or type(val_attr) is str):
        return val_attr
    return val


def normalize_data_tuple(data: tuple) -> tuple[UnSafeDataType, ...]:
    """Convert all non-reference elements in a tuple into strict primitive SafeDataType.

    Args:
        data: Tuple containing primitive values or reference tuples.

    Returns:
        Tuple with all primitive/enum values sanitized to pure `int`, `str`, or `None`.
    """
    for col in data:
        t = type(col)
        if not (t is int or t is str or t is tuple or col is None):
            return tuple(col if type(col) is tuple else to_safe_data(col) for col in data)
    return data


class ChangeSet:
    """Encapsulate file change operations, reference resolution, and parsing pipelines.

    A `ChangeSet` (CS) instance represents a single file modification (diff) between two
    project releases. It records operations generated by language parsers,
    manages reference resolution routes (`REF_ROOT`, `REF_C_AST`, `REF_OLD`), and submits
    resolved operations to the Table Engine (`G.TE`).
    """

    @G.type_check(Self, (str, None), (str, None), (str, None))
    def __init__(
        self,
        operation: str | None = None,
        current_path: str | None = None,
        old_path: str | None = None,
    ) -> None:
        """Initialize ChangeSet state from git diff status line or path arguments.

        Args:
            operation: Git diff status line (e.g. `"M\\tinclude/linux/lock.h"` or
                       `"R100\\told_path.c\\tnew_path.c"`), or operation string.
            current_path: Relative target file path.
            old_path: Relative old file path (for git renames).
        """
        if current_path is None and operation is not None:
            cut_file = operation.split("\t")
            self.file_operation = sys.intern(cut_file[0]) if cut_file[0] else None
            if len(cut_file) == 1:
                pass
            elif len(cut_file) == 2:  # noqa: PLR2004
                current_path = cut_file[1]
            else:
                old_path = cut_file[1]
                current_path = cut_file[2]
        else:
            self.file_operation = sys.intern(operation) if operation else None
        self.current_path = sys.intern(current_path) if current_path else None
        self.old_path = sys.intern(old_path) if old_path else None
        self.cs: list[OperationType] = []
        self.cs_processed: bool = False
        self.cs_result: list[tuple[SafeDataType, ...]] = []
        self.file: Any | None = None
        self.store_dict: dict[Any, Any] = {
            REF_MULTI: [],
        }
        self.gp: Any | None = None
        self.mf: Any | None = None
        self.route: list[LinkType] = [REF_ROOT]
        self.route_count: list[int] = []
        self.multi_stack: list[int] = []
        self._cached_route: tuple[LinkType, ...] | None = (REF_ROOT,)
        self.prior_tags: Any | None = None
        self.parsers: dict[str, Any] = {}
        self.debug: list[Any] = []
        self._bridge_maps: set[tuple[Any, Any]] = set()
        self.profiler: PipelineProfiler | None = (
            PipelineProfiler(file_path=self.current_path or "") if G.PROFILING_ENABLED else None
        )

    @G.type_check(Self, {LinkType})
    def __call__(self, *links: LinkType) -> Self:
        """Push link context scopes (e.g., `REF_C_AST`, `REF_OLD`) onto active routing stack.

        Args:
            *links: One or more route link markers to push.

        Usage:
            `with CS(REF_C_AST):`
            `with CS(REF_OLD):`
            `with CS(REF_MULTI):`

        Returns:
            Self (Context manager).
        """
        self._cached_route = None
        if len(links) == 1:
            self.route_count.append(1)
            self.route.append(links[0])
            # REF_MULTI handling
            if links[0] == REF_MULTI:
                self.route_count[-1] = 2
                multi_idx = len(self.store_dict[REF_MULTI])
                self.route.append(multi_idx)
                self.store_dict[REF_MULTI].append([])
                self.multi_stack.append(multi_idx)
            return self

        self.route_count.append(len(links))
        for link in links:
            self.route.append(link)
        return self

    def __enter__(self) -> Self:
        """Enter route context manager block (`with CS(REF_C_AST):`)."""
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception_value: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        """Exit route context manager block and pop pushed link scopes."""
        self._cached_route = None
        count = self.route_count.pop()
        popped = self.route[-count:] if len(self.route) >= count else []
        if REF_MULTI in popped and self.multi_stack:
            self.multi_stack.pop()
        for _ in range(count):
            self.route.pop()
            if len(self.route) == 0:
                self.route = [REF_ROOT]
                self._cached_route = (REF_ROOT,)

    def last_not_none(self) -> None:
        """Sanity check verifying that the last operation in `CS.cs` is not `None`.

        Raises:
            CONTINUE_EXCEPTION: If the last operation in `CS.cs` is None.
        """
        if not self.cs or self.cs[-1] is None:
            logger.error("Last not None Error")
            raise CONTINUE_EXCEPTION

    @G.type_check(Self, PointerType, RouteType)
    def resolve_ref(self, query: PointerType, parsed_route: RouteType) -> SafeDataType | list[SafeDataType]:
        """Resolve a pointer query using parsed route to locate value across CS index or external CS.

        Evaluates route links:
        - If `REF_FILE` link is present, redirects lookup to another file's ChangeSet in `gp.ChangeSet_Dict`.
        - If `REF_POS` link is present, fetches directly from `CS.cs` at the specified position.
        - If `REF_MULTI` link is present, will return an array.
        - Otherwise looks up `parsed_route` in `self.store_dict` to find stored position.

        Args:
            query: PointerType `(table_id, col_idx)`.
            parsed_route: Canonical normalized route link tuple.

        Returns:
            Resolved primitive value (`int`, `str`, `None`).
        """
        if parsed_route[0] == REF_NO_REF:
            return None

        if parsed_route[0] == REF_FILE:
            foreign_cs = self.gp.safe_get_cs(parsed_route[1])
            return foreign_cs.resolve_ref(query, parsed_route[2:])

        if parsed_route[0] == REF_POS:
            return self._get_value_at(query, parsed_route[1])

        if parsed_route[0] == REF_MULTI:
            return self._get_values_at(query, self._get_multi_from_route(parsed_route))

        return self._get_value_at(query, self._get_pos_from_route(parsed_route, query[0]))

    @G.type_check(Self, tuple)
    def _resolve_ref_from_tuple(self, data: tuple) -> tuple[SafeDataType, ...]:
        """Resolve all reference tuples in a data tuple into safe primitive values.

        Evaluates each element in `data`. If an element is a reference tuple `(query, OP_REF, route)`,
        calls `resolve_ref(query, route)`. All resolved values are de-subclassed to native primitives.

        Args:
            data: Column values tuple, which may include unresolved reference tuples.

        Returns:
            Tuple of resolved primitive values (`tuple[SafeDataType, ...]`).

        Raises:
            REF_NOT_RESOLVABLE: If any reference tuple cannot be resolved.
        """
        for col in data:
            if type(col) is tuple:
                break
        else:
            return data

        output_data = []
        out_append = output_data.append
        cs_res = self.cs_result
        cs_res_len = len(cs_res)

        for val in data:
            if type(val) is tuple:
                if len(val) == 3 and val[1] == OP_REF:
                    route = val[2]
                    if type(route) is tuple and len(route) == 2 and route[0] == REF_POS:
                        pos_idx = route[1]
                        col_idx = val[0][1]
                        if pos_idx < cs_res_len:
                            res = cs_res[pos_idx]
                            if res is not None and col_idx < len(res):
                                resolved = res[col_idx]
                                if type(resolved) is not tuple:
                                    out_append(resolved)
                                    continue
                resolved = self.resolve_ref(val[0], val[2])
                if resolved is None:
                    if G.BP_ON_REF_FAIL:
                        G.BP()
                    raise REF_NOT_RESOLVABLE
                out_append(to_safe_data(resolved))
            else:
                out_append(val if (type(val) is int or type(val) is str or val is None) else to_safe_data(val))
        return tuple(output_data)

    def execute(self) -> bool:
        """Execute queued operations in `CS.cs` sequentially against Table Engine (`G.TE`).

        Iterates over `CS.cs` starting from index `len(CS.cs_result)`. Resolves any embedded
        references, unpacks dynamic AST view references (`OP_REF_VIEW`), submits operations to
        `G.TE.set()`, `G.TE.update()`, or `G.TE.view_set()`, and records execution outputs in
        `CS.cs_result`. Sets `cs_processed = True` upon completion.

        Returns:
            True if all operations were successfully resolved and executed, False if unresolved.
        """
        if self.cs_processed:
            return True

        te = G.TE
        operation_offset = len(self.cs_result)
        t_exec_0 = time.perf_counter() if self.profiler is not None else 0.0
        cs = self.cs
        cs_result = self.cs_result
        cs_res_append = cs_result.append
        te_set = te.set
        te_update = te.update
        te_view_set = te.view_set

        for operation in cs[operation_offset:]:
            try:
                op_type = operation[1]
                if op_type == OP_REF_VIEW:
                    unpacked = self._unpack_ref_view(operation)
                    if unpacked is None:
                        raise REF_NOT_RESOLVABLE
                    operation = unpacked
                    op_type = operation[1]

                data = self._resolve_ref_from_tuple(operation[2])

                if op_type == OP_DONE or op_type == OP_VIEW_DONE:
                    cs_res_append(data)
                    continue

                if op_type == OP_SET:
                    cs_res_append(te_set(operation[0], data))
                    continue
                if op_type == OP_UPDATE:
                    cs_res_append(te_update(operation[0], data))
                    continue
                if op_type == OP_VIEW_SET:
                    cs_res_append(te_view_set(operation[0], data))
                    continue

                logger.error(f"ERROR, UNKNOWN OPERATION {operation}")
                cs_res_append(None)
            except REF_NOT_RESOLVABLE:
                if self.profiler is not None:
                    self.profiler.cs_execute_s = time.perf_counter() - t_exec_0
                return False

        if self.profiler is not None:
            self.profiler.cs_execute_s = time.perf_counter() - t_exec_0

        self.cs_processed = True
        return True

    @G.type_check(Self, OperationType, {LinkType})
    def store(self, operation: OperationType, *route: LinkType) -> None:
        """Queue an operation into `CS.cs` and index its position in `store_dict` under parsed route.

        Args:
            operation: Operation tuple `(target, op_type, data)`.
            *route: Optional route link markers modifying the active context route.

        Usage:
            `CS.store(m_file_name.set(None, CS.current_path))`
            `CS.store(m_ast.view(...), REF_C_AST)`
        """
        if self.file_operation is None:
            self.cs.append(operation)
            return

        if route:
            if route[-1] == REF_POS:
                self.route_count.append(2)
                op_idx = len(self.cs)
                if self.multi_stack and REF_NO_REF not in self.route and REF_NO_REF not in route:
                    multi_idx = self.multi_stack[-1]
                    if isinstance(multi_idx, int) and len(self.store_dict[REF_MULTI]) > multi_idx:
                        if self.store_dict[REF_MULTI][multi_idx] is None:
                            self.store_dict[REF_MULTI][multi_idx] = []
                        self.store_dict[REF_MULTI][multi_idx].append(op_idx)
                self.route.append(REF_POS)
                self.route.append(op_idx)
                self._cached_route = None
                self.cs.append(operation)
                return
            elif route[-1] == REF_NO_REF:
                self.cs.append(operation)
                return

        elif self.route[-1] == REF_POS:
            self.route_count[-1] = 2
            op_idx = len(self.cs)
            if self.multi_stack and REF_NO_REF not in self.route:
                multi_idx = self.multi_stack[-1]
                if isinstance(multi_idx, int) and len(self.store_dict[REF_MULTI]) > multi_idx:
                    if self.store_dict[REF_MULTI][multi_idx] is None:
                        self.store_dict[REF_MULTI][multi_idx] = []
                    self.store_dict[REF_MULTI][multi_idx].append(op_idx)
            self.route.append(op_idx)
            self._cached_route = None
            self.cs.append(operation)
            return
        elif self.route[-1] == REF_NO_REF:
            self.cs.append(operation)
            return

        if not route:
            parsed_route = self._cached_route
            if parsed_route is None:
                parsed_route = tuple(self.route_parse(self.route))
                self._cached_route = parsed_route
        else:
            parsed_route = tuple(self.route_parse(self.route + list(route)))

        target = operation[0]
        first_tid = target if type(target) is int else target[0][0][0]

        if parsed_route[0] == REF_MULTI:
            if G.DEBUG_TYPECHECK:
                assert len(self.store_dict[REF_MULTI]) > parsed_route[1], "CS.store() is trying to store into a non-existing REF_MULTI"
            if self.store_dict[REF_MULTI][parsed_route[1]] is None:
                self.store_dict[REF_MULTI][parsed_route[1]] = []

            self.store_dict[REF_MULTI][parsed_route[1]].append(len(self.cs))
        else:
            store_entry = self.store_dict.get(parsed_route)
            if store_entry is None:
                store_entry = {}
                self.store_dict[parsed_route] = store_entry
            elif G.DEBUG_TYPECHECK:
                if (current_val := store_entry.get(first_tid)) is not None:
                    assert self.cs[current_val] == operation, (
                        f"Not only did you push 2 times to the same route, but you didn't push the same value!!!\n"
                        f"-current_val:{self.cs[current_val]}\n-operation:{operation}"
                    )

            store_entry[first_tid] = len(self.cs)

        self.cs.append(operation)

    def get_route_parse(self) -> tuple:
        """Return the normalized canonical route tuple for current routing stack."""
        return tuple(self.route_parse(self.route))

    @G.type_check(Self, RouteType)
    def route_parse(self, route: RouteType) -> list:
        """Normalize a route link list into a minimal canonical route representation.

        Args:
            route: Sequence of route links.

        Returns:
            Normalized list of route link markers.
        """
        parsed_route = []
        data_bypass = False
        has_ref_file = None

        for link in route:
            if data_bypass:
                if has_ref_file is False:
                    has_ref_file = link
                else:
                    parsed_route.append(link)
                data_bypass = False
                continue

            if link == REF_FILE:
                has_ref_file = False
                data_bypass = True
                parsed_route.clear()
                continue

            if link in _REF_BYPASS_LINKS:
                data_bypass = True
                parsed_route.clear()

            if link in _REF_RESET_LINKS:
                parsed_route.clear()

            parsed_route.append(link)

        if has_ref_file is not None:
            parsed_route = [REF_FILE, has_ref_file] + parsed_route
        return parsed_route

    @G.type_check(Self, RouteType, int)
    def _get_pos_from_route(self, route: RouteType, table_id: int) -> int | None:
        """Retrieve stored operation index in `CS.cs` matching pre-parsed route and table_id.

        Args:
            route: Pre-parsed route tuple.
            table_id: Target table identifier integer.

        Returns:
            Operation index integer or None.
        """
        if G.DEBUG_TYPECHECK:
            route_tuple = tuple(route)
            route_parsed = tuple(self.route_parse(route_tuple))
            assert route_tuple == route_parsed, "get_pos_from_route didn't receive a clean route, could be reduced with route_parse()"

        store_entry = self.store_dict.get(tuple(route))
        return store_entry.get(table_id) if store_entry is not None else None

    @G.type_check(Self, RouteType)
    def _get_multi_from_route(self, route: RouteType) -> list:
        """Retrieve stored operation indices in `CS.cs` matching REF_MULTI route.

        Args:
            route: Route ending with `(REF_MULTI, pos)`.

        Returns:
            List of operation indices in `CS.cs`.
        """
        if G.DEBUG_TYPECHECK:
            assert len(route) > 1, "get_multi_from_route got a route that is too small"
            assert isinstance(route[-1], int), "get_multi_from_route got non-int at pos -1"

        return self.store_dict[REF_MULTI][route[-1]]

    @G.type_check(Self, PointerType, {int, None})
    def _get_value_at(self, query: PointerType, pos: int | None) -> SafeDataType:
        """Fetch resolved column value at query pointer `(table_id, col_idx)` from operation at index `pos`.

        Args:
            query: PointerType `(table_id, col_idx)`.
            pos: Numerical index of the operation in `CS.cs`.

        Returns:
            The resolved value (`int`, `str`, `None`) at that position and column.
        """
        if pos is None:
            return None

        operation = self.cs[pos]

        if len(self.cs_result) > pos:
            data = self.cs_result[pos]
        else:
            data = operation[2]

        if G.DEBUG_TYPECHECK:
            target = operation[0]
            tableid = target if type(target) is int else target[0][0][0]
            data_size = len(data)

            if operation[1] in {OP_VIEW_DONE, OP_VIEW_SET, OP_REF_VIEW}:
                if operation[1] == OP_REF_VIEW:
                    data_size -= 1

            assert query[0] == tableid, f"_get_value_at()'s query doesn't match underlying OP TableID.\nquery:{query} pos:{pos}"
            assert data_size > query[1], f"_get_value_at()'s query column is too big for the OP.\nquery:{query} pos:{pos}"

        result = data[query[1]]
        if type(result) is tuple:
            return None
        return to_safe_data(result)

    @G.type_check(Self, PointerType, list)
    def _get_values_at(self, query: PointerType, multipos: list) -> list[SafeDataType]:
        """Fetch resolved column values at query pointer `(table_id, col_idx)` across multiple positions.

        Args:
            query: PointerType `(table_id, col_idx)`.
            multipos: List of numerical indices in `CS.cs`.

        Returns:
            List of resolved primitive values.
        """
        return_list = []
        for pos in multipos:
            operation = self.cs[pos]

            if len(self.cs_result) > pos:
                data = self.cs_result[pos]
            else:
                data = operation[2]

            if G.DEBUG_TYPECHECK:
                target = operation[0]
                tableid = target if type(target) is int else target[0][0][0]
                data_size = len(data)

                if operation[1] in {OP_VIEW_DONE, OP_VIEW_SET, OP_REF_VIEW}:
                    if operation[1] == OP_REF_VIEW:
                        data_size -= 1

                assert query[0] == tableid, f"_get_values_at()'s query doesn't match underlying OP TableID.\nquery:{query} pos:{pos}"
                assert data_size > query[1], f"_get_values_at()'s query column is too big for the OP.\nquery:{query} pos:{pos}"

            result = data[query[1]]
            if type(result) is tuple:
                return_list.append(None)
            else:
                return_list.append(to_safe_data(result))
        return return_list

    @G.type_check(Self, RouteType, {int, None})
    def get_available_data(
        self,
        route: RouteType,
        tableid: int | None = None,
    ) -> list[tuple[OperationType, tuple[SafeDataType, ...] | None]]:
        """Get operation and result for a given route (assumes no REF_FILE).

        Args:
            route: Route link tuple.
            tableid: Target table identifier integer (optional for REF_POS/REF_MULTI).

        Returns:
            List of `(operation, processed_result_tuple)` pairs.
        """
        result = []
        get_target = []

        if route[0] == REF_POS:
            get_target.append(route[1])
        elif route[0] == REF_MULTI:
            get_target.extend(self._get_multi_from_route(route))
        else:
            get_target.append(self._get_pos_from_route(route, tableid))

        for target in get_target:
            if target is not None and 0 <= target < len(self.cs):
                target_processed = self.cs_result[target] if target < len(self.cs_result) else None
                if target_processed is None:
                    result.append((self.cs[target], None))
                else:
                    result.append((self.cs[target], tuple(to_safe_data(x) for x in target_processed)))

        return result

    @G.type_check(Self, PointerType, {LinkType})
    def ref(self, query: PointerType, *route_args: LinkType) -> UnSafeDataType:
        """Create reference tuple `(query, OP_REF, route)` or return immediate resolved value.

        Args:
            query: PointerType `(table_id, col_idx)`.
            *route_args: Route links defining lookup context.

        Usage:
            `CS.ref(m_file_name.fnid)`
            `CS.ref(m_ast.ast_id, REF_C_AST)`

        Returns:
            Resolved primitive value if already resolvable, or reference tuple `(query, OP_REF, route)`.
        """
        if route_args:
            if self.route[-1] == REF_POS and route_args[0] == REF_POS:
                parsed_route = tuple(self.route_parse(route_args))
            else:
                parsed_route = tuple(self.route_parse(self.route + list(route_args)))
        else:
            parsed_route = tuple(self.route_parse(self.route))

        result = self.resolve_ref(query, parsed_route)
        if result is not None:
            return to_safe_data(result)

        return (query, OP_REF, parsed_route)

    def _resolve_col_val(self, target_op, val_tuple, col_idx: int) -> SafeDataType:
        """Extract resolved column value from val_tuple or target_op[2] fallback.

        Args:
            target_op: Underlying operation tuple.
            val_tuple: Processed result tuple.
            col_idx: Target column position integer.

        Returns:
            Resolved primitive value.
        """
        val = None
        if val_tuple is not None and col_idx < len(val_tuple):
            val = val_tuple[col_idx]

        if val is None or (isinstance(val, tuple) and len(val) == 3 and val[1] == OP_REF):
            if target_op is not None and len(target_op) > 2 and col_idx < len(target_op[2]):
                val = target_op[2][col_idx]

        if isinstance(val, tuple) and len(val) == 3 and val[1] == OP_REF:
            val = self.resolve_ref(val[0], val[2])

        return to_safe_data(val)

    def _unpack_ref_view(self, operation: OperationType) -> OperationType | None:
        """Expand dynamic AST view schema reference (`OP_REF_VIEW`) into concrete `OP_VIEW_SET`.

        Evaluates schema rules against AST nodes stored in `store_dict`, matches applicable rule,
        builds joined table structure, and returns concrete `(joins_tuple, OP_VIEW_SET, data_tuple)`.

        Args:
            operation: `(joins, OP_REF_VIEW, (..., schema))` operation tuple.

        Returns:
            Concrete `(joins, OP_VIEW_SET, data)` operation or None if unresolved.
        """
        joins = list(operation[0])
        data = [to_safe_data(x) for x in operation[2][:-1]]

        schema = operation[2][-1]
        schema_ifs = schema[0]
        schema_thens = schema[1]
        schema_route = schema[2]
        rank = schema[3] if len(schema) > 3 else 0

        if not schema_route:
            data_list = []
        else:
            target_table_id = schema_ifs[0][0][0] if isinstance(schema_ifs[0][0], tuple) else schema_ifs[0][0]
            if schema_route[0] == REF_FILE:
                foreign_cs = self.gp.safe_get_cs(schema_route[1])
                data_list = foreign_cs.get_available_data(schema_route[2:], target_table_id)
            else:
                data_list = self.get_available_data(schema_route, target_table_id)

        if not data_list:
            return (tuple(joins), OP_VIEW_SET, tuple(data))

        for target_op, val_tuple in data_list:
            chosen_rule = -1
            for i, rule in enumerate(schema_ifs):
                testing_val = self._resolve_col_val(target_op, val_tuple, rule[0][1])
                if testing_val is None:
                    continue

                expected = rule[1]
                is_match = (testing_val in expected) if isinstance(expected, (tuple, list, set, dict)) else (testing_val == expected)
                if is_match:
                    chosen_rule = i
                    break

            rule_joins = schema_thens[chosen_rule][0] if chosen_rule != -1 else schema_thens[-1][0]
            rule_items = schema_thens[chosen_rule][1] if chosen_rule != -1 else schema_thens[-1][1]

            for item in rule_items:
                if isinstance(item, tuple):
                    if G.is_PointerType(item):
                        val = self._resolve_col_val(target_op, val_tuple, item[1])
                        if val is None:
                            return None
                        data.append(to_safe_data(val))
                        continue
                    elif len(item) == 1 and item[0] == "rank":
                        data.append(rank)
                        rank += 1
                        continue
                data.append(to_safe_data(item))

            for join in rule_joins:
                PointerGetter.add_join(joins, join)

        return (tuple(joins), OP_VIEW_SET, tuple(data))

    def parse(self) -> None:
        """Select and invoke appropriate language AST parser based on current_path file type.

        Detects file language type using `type_check(current_path)` and triggers parser (e.g. `c_ast_parse(self)`).
        """
        if not self.current_path or self.file_operation == "R100":
            return

        try:
            current_type = type_check(self.current_path)
            if current_type == T_C:
                c_ast_parse(self)
            elif current_type == T_ASM:
                asm_ast_parse(self)
            elif current_type == T_KCONFIG:
                kconfig_ast_parse(self)
            elif current_type == T_MAINTAINERS:
                from parser.maintainer_ast.maintainer_ast import maintainer_ast_parse
                maintainer_ast_parse(self)
            elif current_type == T_CREDITS:
                from parser.maintainer_ast.maintainer_ast import credits_ast_parse
                credits_ast_parse(self)
            elif current_type == T_RUST:
                from parser.rust_ast.rust_ast import rust_ast_parse
                rust_ast_parse(self)
            elif current_type == T_RAW:
                from parser.raw_ast.raw_ast import raw_ast_parse
                raw_ast_parse(self)
            else:
                from parser.raw_ast.raw_ast import raw_ast_parse
                raw_ast_parse(self)
        except FILE_ERROR as e:
            logger.error(f"FILE_ERROR for '{self.file_operation}'={self.current_path}")
            logger.error(e)
            self.cs = []

    def clear_bloat(self) -> None:
        """Remove un-picklable and ephemeral parsing structures before IPC serialization."""
        self.gp = None
        self.mf = None
        self.debug = []
        self.parsers = {}
        self.prior_tags = None
        self.prior_tags_map = None
        self.active_tag_list = None
        self._bridge_maps = set()

    def register_bridge_map(self, tag_ref: Any, map_ref: Any) -> bool:
        """Register a tag-to-map bridge association within this ChangeSet.

        Returns:
            True if this is the first registration of (tag_ref, map_ref),
            False if it has already been registered in this ChangeSet.
        """
        key = (tag_ref, map_ref)
        if key in self._bridge_maps:
            return False
        self._bridge_maps.add(key)
        return True

    def __str__(self) -> str:
        """Return formatted string summarizing file operation, queued cs, and results."""
        result = f"CS:file({self.current_path}),op({self.file_operation}),"
        result += f"cs({','.join(map(str, self.cs))}),"
        result += f"cs_result({','.join(map(str, self.cs_result))})"
        return result


class Table:
    """Represent database table schema and provide operation builders (set, update, view)."""

    def __init__(
        self,
        *,
        table_id: int,
        table_name: str,
        columns: tuple[tuple[str, ...], ...],
        primary: tuple[str, ...],
        foreign: tuple[tuple[str, str, str], ...] | None = None,
        initial_insert: tuple[tuple[SafeDataType, ...], ...] | tuple[SafeDataType, ...] | None = None,
        no_duplicate: bool = False,
        te_cached: bool = False,
        hashing_table: bool | str = False,
    ) -> None:
        """Initialize a Table schema definition, generate dynamic column pointers, and bind to parser.

        Args:
            table_id: Integer index identifying the table in `gp.Table_Array` (e.g., 0, 1, 2...).
            table_name: Database table name string (e.g., `"m_file_name"`).
            columns: Tuple of column definition tuples: `(("col_name", "DATA_TYPE", "CONSTRAINTS"), ...)`.
            primary: Tuple of column name strings forming the Primary Key: `("fnid",)` or `("vid", "fnid")`.
            foreign: Optional tuple of Foreign Key constraints: `(("local_col", "foreign_table", "foreign_col"), ...)`.
            initial_insert: Optional tuple of default rows inserted when table is created.
            no_duplicate: If True, `set()` automatically checks if a row exists in Table Engine (`G.TE`) via `get_set()`.
            te_cached: Pre-loading caching strategy for Table Engine initialization.
            hashing_table: Name of the linked hashing table.

        Side Effects:
            1. Binds column attribute pointers directly on this instance:
               `self.<column_name> = (table_id, col_idx)`  (e.g., `m_file_name.fnid` becomes `(1, 0)`).
            2. Injects this `Table` instance directly into the `parser.c_ast.c_ast_type` module namespace
               under `self.table_name` for direct access during AST node extraction.
        """
        self.table_id = table_id
        self.table_name = table_name
        self.init_columns = columns
        self.length = len(columns)
        self.init_primary = primary

        # Convert primary key column names into 0-indexed column position integers
        temp_primary = []
        for prim in self.init_primary:
            for x, column in enumerate(self.init_columns):
                if column[0] == prim:
                    temp_primary.append(x)
                    break
        self.primary = tuple(temp_primary)

        self.init_foreign = foreign
        self.initial_insert = initial_insert
        self.no_duplicate = no_duplicate
        self.te_cached = te_cached
        self.hashing_table = hashing_table
        self.has_auto_increment = any(any("AUTO_INCREMENT" in str(elem) for elem in col) for col in self.init_columns)

        # Step 1: Bind column attribute pointers directly onto instance (self.column_name = (table_id, col_idx))
        for x, column in enumerate(self.init_columns):
            setattr(self, column[0], (self.table_id, x))

        # Step 2: Inject Table object reference into c_ast_type and c_ast module scopes for AST parsing
        if mod := sys.modules.get("parser.c_ast.c_ast_type"):
            setattr(mod, self.table_name, self)
        if mod_c := sys.modules.get("parser.c_ast.c_ast"):
            setattr(mod_c, self.table_name, self)

    def start_te(self) -> None:
        """Register table schema with active Table Engine (`G.TE`)."""
        G.TE.start(self, G.DB)

    @G.type_check(Self, {UnSafeDataType})
    def set(self, *columns: UnSafeDataType) -> OperationType:
        """Construct OP_SET operation (or get_set if `no_duplicate` is enabled).

        Args:
            *columns: Row column values.

        Usage:
            `m_file_name.set(None, CS.current_path)`

        Returns:
            Operation tuple `(table_id, OP_SET, columns)`.
        """
        if self.no_duplicate:
            return self.get_set(*columns)

        sanitized_columns = normalize_data_tuple(columns)
        return (self.table_id, OP_SET, sanitized_columns)

    @G.type_check(Self, {UnSafeDataType})
    def update(self, *columns: UnSafeDataType) -> OperationType:
        """Construct OP_UPDATE operation.

        Args:
            *columns: Row column values (or reference tuples) matching table schema.

        Returns:
            Operation tuple `(table_id, OP_UPDATE, columns)`.
        """
        if is_data_unsafe(columns):
            return (self.table_id, OP_UPDATE, normalize_data_tuple(columns))

        sanitized_columns = tuple(to_safe_data(col) for col in columns)

        if None not in sanitized_columns:
            return (self.table_id, OP_UPDATE, sanitized_columns)

        get_columns = tuple(
            sanitized_columns[i] if i in self.primary else None
            for i in range(len(sanitized_columns))
        )

        get_result = G.TE.get(self.table_id, get_columns)

        if get_result is not None:
            sanitized_columns = tuple(
                sanitized_columns[i] if sanitized_columns[i] is not None else (get_result[i] if i < len(get_result) else None)
                for i in range(len(sanitized_columns))
            )

        return (self.table_id, OP_UPDATE, sanitized_columns)

    @G.type_check(Self, {SafeDataType})
    def get(self, *columns: SafeDataType) -> OperationType | None:
        """Query Table Engine for matching record and return OP_DONE operation, or None.

        Args:
            *columns: Column filter values.

        Usage:
            `m_file_name.get(None, "core/sched.c")`

        Returns:
            Operation tuple `(table_id, OP_DONE, result)` or None.
        """
        if is_data_unsafe(columns):
            logger.error(f"""An {self.table_name}.get was done with unresolved refs,
            This is unexpected behavior. CRASH""")
            logger.error(columns)
            G.emergency_shutdown(55)

        sanitized_columns = tuple(to_safe_data(col) for col in columns)
        result = G.TE.get(self.table_id, sanitized_columns)
        if result is None:
            return None

        return (self.table_id, OP_DONE, result)

    @G.type_check(Self, {UnSafeDataType})
    def get_set(self, *columns: UnSafeDataType) -> OperationType:
        """Return existing record as OP_DONE if match exists in TE, else construct OP_SET.

        Args:
            *columns: Column values or reference tuples.

        Usage:
            `m_file_name.get_set(None, "fs/ext4/super.c")`

        Returns:
            Operation tuple `(table_id, OP_DONE, result)` if found, else `(table_id, OP_SET, columns)`.
        """
        if not is_data_unsafe(columns):
            sanitized_columns = tuple(to_safe_data(col) for col in columns)
            result = G.TE.get(self.table_id, sanitized_columns)
            if result:
                return (self.table_id, OP_DONE, result)
            return (self.table_id, OP_SET, sanitized_columns)

        return (self.table_id, OP_SET, normalize_data_tuple(columns))

    @G.type_check(Self, JoinsType, {UnSafeDataType})
    def view(self, joins: JoinsType, *data: UnSafeDataType) -> OperationType:
        """Construct OP_VIEW_SET operation (or OP_VIEW_DONE if view_get finds match).

        Args:
            joins: Relational join graph tuple (JoinsType).
            *data: Column data values across joined tables.

        Usage:
            `m_ast.view(((m_ast.ast_id,),), None, "node_name", type_id)`

        Returns:
            Operation tuple `(joins, OP_VIEW_DONE, result)` if found, else `(joins, OP_VIEW_SET, data)`.
        """
        if not is_data_unsafe(data):
            sanitized_data = tuple(to_safe_data(x) for x in data)
            result = G.TE.view_get(joins, sanitized_data)
            if result:
                return (joins, OP_VIEW_DONE, result)
            return (joins, OP_VIEW_SET, sanitized_data)

        return (joins, OP_VIEW_SET, normalize_data_tuple(data))

    @G.type_check(Self, JoinsType, {SafeDataType})
    def view_get(self, joins: JoinsType, *data: SafeDataType) -> OperationType | None:
        """Query Table Engine for single multi-table view join match and return OP_VIEW_DONE.

        Args:
            joins: Relational join graph tuple (JoinsType).
            *data: Column filter values across joined tables.

        Usage:
            `m_ast.view_get(joins_tuple, None, "struct_name", type_id)`

        Returns:
            Operation tuple `(joins, OP_VIEW_DONE, result)` or None.
        """
        if not is_data_unsafe(data):
            sanitized_data = tuple(to_safe_data(x) for x in data)
            result = G.TE.view_get(joins, sanitized_data)
            if result:
                return (joins, OP_VIEW_DONE, result)

        return None

    @G.type_check(Self, JoinsType, {UnSafeDataType})
    def view_get_multiple(
        self,
        joins: JoinsType,
        *data: UnSafeDataType,
    ) -> list[tuple[SafeDataType, ...]] | None:
        """Query Table Engine for all matching multi-table view join rows.

        Args:
            joins: Relational join graph tuple (JoinsType).
            *data: Column filter values across joined tables.

        Returns:
            List of matching joined row tuples or None if unsafe.
        """
        if is_data_unsafe(data):
            return None
        sanitized_data = tuple(to_safe_data(x) for x in data)
        results = G.TE.view_get_multiple(joins, sanitized_data)
        if results is None:
            return None
        return [tuple(to_safe_data(col) for col in row) for row in results]

    def ref_view(self, joins: JoinsType, *data: Any) -> OperationType:
        """Construct OP_REF_VIEW operation for dynamic schema-driven AST view expansion.

        Args:
            joins: Relational join graph tuple.
            *data: Dynamic view schema arguments.

        Returns:
            Operation tuple `(joins, OP_REF_VIEW, data)`.
        """
        return (joins, OP_REF_VIEW, data)
