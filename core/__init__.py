"""core package - KernelInfo-Parser Core Framework & State Management.

Exports:
    - Global state, types, and constants (`globalstuff`)
    - String manipulation and terminal formatting (`StringWrangler`)
    - Git and filesystem working directory manager (`FileHandler`)
    - Multiprocessing IPC and runtime state container (`GreatProcessor`)
    - Relational schema tables and layout (`DBLayout`)
    - ChangeSet management and operation builders (`TableHandling`)
"""

from core.globalstuff import (
    G,
    COLOR,
    PointerGetter,
    type_check,
    ASTT,
    OP_DONE,
    OP_SET,
    OP_UPDATE,
    OP_REF,
    OP_REF_VIEW,
    OP_VIEW_DONE,
    OP_VIEW_SET,
    REF_ROOT,
    REF_OLD,
    REF_POS,
    REF_FILE,
    REF_MULTI,
    REF_C_AST,
    REF_NO_REF,
    T_DIR,
    T_C,
    T_KCONFIG,
    T_RUST,
    FILE_ERROR,
    REF_NOT_RESOLVABLE,
    CONTINUE_EXCEPTION,
    PointerType,
    JoinType,
    JoinsType,
    OperationType,
    LinkType,
    RouteType,
    RefType,
    SafeDataType,
    UnSafeDataType,
)

from core.StringWrangler import (
    wrap_lines,
    render_ansi_box,
    render_with_indent,
    align_columns,
    tag_lines,
    group_lines,
    normalize,
    listify,
)

from core.FileHandler import MasterFile
from core.GreatProcessor import GreatProcessor
from core.TableHandling import (
    Table,
    ChangeSet,
    to_safe_data,
    is_data_unsafe,
    normalize_data_tuple,
)
from core.DBLayout import (
    init_db_layout,
    TABLES,
    m_v_main,
    m_file_name,
    m_file,
    m_bridge_file,
    m_moved_file,
    m_type_descriptor,
    m_ast,
    m_ast_container,
    m_ast_include,
    m_ast_debug,
    m_tag,
    m_bridge_tag,
    m_map_ast,
    m_bridge_map,
)

__all__ = [
    # Global
    "G",
    "COLOR",
    "PointerGetter",
    "type_check",
    "ASTT",
    "OP_DONE",
    "OP_SET",
    "OP_UPDATE",
    "OP_REF",
    "OP_REF_VIEW",
    "OP_VIEW_DONE",
    "OP_VIEW_SET",
    "REF_ROOT",
    "REF_OLD",
    "REF_POS",
    "REF_FILE",
    "REF_MULTI",
    "REF_C_AST",
    "REF_NO_REF",
    "T_DIR",
    "T_C",
    "T_KCONFIG",
    "T_RUST",
    "FILE_ERROR",
    "REF_NOT_RESOLVABLE",
    "CONTINUE_EXCEPTION",
    "PointerType",
    "JoinType",
    "JoinsType",
    "OperationType",
    "LinkType",
    "RouteType",
    "RefType",
    "SafeDataType",
    "UnSafeDataType",
    # StringWrangler
    "wrap_lines",
    "render_ansi_box",
    "render_with_indent",
    "align_columns",
    "tag_lines",
    "group_lines",
    "normalize",
    "listify",
    # FileHandler
    "MasterFile",
    # GreatProcessor
    "GreatProcessor",
    # TableHandling
    "Table",
    "ChangeSet",
    "to_safe_data",
    "is_data_unsafe",
    "normalize_data_tuple",
    # DBLayout
    "init_db_layout",
    "TABLES",
    "m_v_main",
    "m_file_name",
    "m_file",
    "m_bridge_file",
    "m_moved_file",
    "m_type_descriptor",
    "m_ast",
    "m_ast_container",
    "m_ast_include",
    "m_ast_debug",
    "m_tag",
    "m_bridge_tag",
    "m_map_ast",
    "m_bridge_map",
]
