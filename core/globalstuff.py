"""globalstuff.py - Global objects and constants."""
import os
import sys
import shutil
import logging
import re
from pathlib import Path
from core.StringWrangler import wrap_lines, render_ansi_box, render_with_indent
import contextlib
import time
from typing import Self
from functools import wraps
import hashlib
from enum import IntEnum, auto, Flag


OP_DONE, OP_SET, OP_UPDATE, OP_REF, OP_REF_VIEW, OP_VIEW_DONE, OP_VIEW_SET = range(7)
REF_ROOT, REF_OLD, REF_POS, REF_FILE, REF_MULTI, REF_C_AST, REF_NO_REF = range(7)
T_DIR, T_C, T_KCONFIG, T_RUST, T_ASM, T_MAINTAINERS, T_CREDITS, T_RAW = range(8)

PointerType = tuple[int, int]
JoinType = tuple[PointerType, PointerType, int] | tuple[PointerType]
JoinsType = tuple[JoinType, ...]

OperationType = tuple[JoinsType | int, int, tuple]

LinkType = int | str
RouteType = tuple[LinkType, ...] | list[LinkType, ...]
RefType = tuple[PointerType, int, RouteType]

SafeDataType = int | str | bytes | None
UnSafeDataType = SafeDataType | RefType


import re


def compute_code_hash(code: str) -> bytes:
    """Compute deterministic 32-byte binary SHA-256 hash for code snippet string."""
    return hashlib.sha256(code.encode("latin-1")).digest()


def normalize_repo_path(path: str, base_dir: str | None = None) -> str:
    """Normalize absolute host/RAMDISK file paths into relative git repository paths."""
    if not path:
        return ""
    s = path.strip()
    if (s.startswith("<") and s.endswith(">")) or (s.startswith('"') and s.endswith('"')):
        return s

    norm = os.path.normpath(s)
    base = base_dir or getattr(G, "CURRENT_PARSING_DIR", None)
    if base:
        b_norm = os.path.normpath(base)
        if norm.startswith(b_norm + "/"):
            return norm[len(b_norm) + 1:]
        if norm == b_norm:
            return ""

    # Check for temporary clone directories in RAMDISK (/dev/shm or /tmp)
    m = re.match(r"^(?:/dev/shm|/tmp)/(?:code-parser\.[^/]+(?:/linux)?/|[^/]+/linux/|[^/]+/)?(.*)$", norm)
    if m and m.group(1):
        return m.group(1)

    if norm.startswith("/") and "/linux/" in norm:
        return norm.split("/linux/", 1)[1]

    if norm.startswith("/") and not norm.startswith(("/usr/", "/opt/", "/etc/", "/lib")):
        return norm.lstrip("/")

    return norm


_UNNAMED_PATTERN = re.compile(r"(\((?:unnamed|anonymous) at )([^)]+)(\))")


def clean_unnamed_spelling(spelling: str) -> str:
    """Normalize Libclang unnamed/anonymous cursor spellings by stripping host/RAMDISK prefixes down to relative git path."""
    if not spelling or ("(unnamed at " not in spelling and "(anonymous at " not in spelling):
        return spelling

    def repl(m: re.Match[str]) -> str:
        prefix, full_loc, suffix = m.group(1), m.group(2), m.group(3)
        parts = full_loc.rsplit(":", 2)
        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            path_part, loc = parts[0], f":{parts[1]}:{parts[2]}"
        elif len(parts) >= 2 and parts[-1].isdigit():
            path_part, loc = full_loc.rsplit(":", 1)[0], f":{full_loc.rsplit(':', 1)[1]}"
        else:
            path_part, loc = full_loc, ""

        current_file = getattr(G, "CURRENT_PARSING_FILE", None)
        if current_file and path_part.endswith(current_file):
            rel_path = current_file
        else:
            rel_path = normalize_repo_path(path_part)

        return f"{prefix}{rel_path}{loc}{suffix}"

    return _UNNAMED_PATTERN.sub(repl, spelling)


class FILE_ERROR(Exception):  # noqa: D101, N801, N818
    pass
class REF_NOT_RESOLVABLE(Exception):  # noqa: D101, N801, N818
    pass
class CONTINUE_EXCEPTION(Exception):  # noqa: D101, N801, N818
    pass

class GlobalStuff:
    """Global object. Contain DB and TE."""

    def __init__(self) -> None:
        """Set default for all but DB and TE."""
        self.DB = None
        self.TE = None
        self.MF = None
        self.RAMDISK = "/dev/shm"  # noqa: S108
        self.CPUS = 8
        # Placeholder until main() / webapp config select the configured repository (core.config.get_repository)
        self.linux_directory = Path(__file__).resolve().parent.parent / "repos" / "linux"
        self.CLEAN_PRINT = True

        self.BP_ON_SHUTDOWN = False
        self.BP_ON_REF_FAIL = False

        # Active file context for logging
        self.CURRENT_PARSING_FILE: str | None = None
        self.CURRENT_PARSING_DIR: str | None = None

        # FAIL CHECK
        self.OVERRIDE_FC_MAX_LOOP_EXEC_MULT = 2
        # FUNCTIONS
        self.OVERRIDE_CPPRO_CINDEX_INPUT = True
        # PRINTS
        # SQL
        self.OVERRIDE_TABLE_CREATION_PRINT = False
        # AST
        self.OVERRIDE_GLOBAL_C_AST = False
        self.OVERRIDE_C_AST_LINE_PRINT = True
        self.OVERRIDE_CPPRO_PRINT = True
        self.OVERRIDE_CINDEX_ERROR_PRINT = True
        self.OVERRIDE_CINDEX_SKIPPED_PRINT = True

        self.OVERRIDE_FORCE_AST_DEBUG = False
        # GIT
        self.OVERRIDE_FORGOTTEN_PRINT = False
        # General Print
        self.OVERRIDE_MAX_PRINT_SIZE = 60

        # TypeCheck
        self.DEBUG_TYPECHECK = False

        # Profiling Facility
        self.PROFILING_ENABLED = False
        self.EXECUTION_DEADLINE: float | None = None
        self.STAGE_PROFILER: any = None

        # Memory Profile Management: "normal" | "low" | "very_low"
        self.MEMORY_MODE: str = "normal"

        self.OP_isinstanceDict = {
            PointerType: self.is_PointerType,
            JoinType: self.is_JoinType,
            JoinsType: self.is_JoinsType,
            OperationType: self.is_OperationType,
            LinkType: self.is_LinkType,
            RouteType: self.is_RouteType,
            RefType: self.is_RefType,
            SafeDataType: self.is_SafeDataType,
            UnSafeDataType: self.is_UnSafeDataType,
            None: lambda x: x is None,
        }

    @property
    def LOW_MEMORY_MODE(self) -> bool:
        """Return True if running in low or very low memory mode."""
        return self.MEMORY_MODE in ("low", "very_low")

    @property
    def VERY_LOW_MEMORY_MODE(self) -> bool:
        """Return True if running in very low memory mode."""
        return self.MEMORY_MODE == "very_low"

    def set_execution_deadline(self, duration_s: float | None) -> None:
        """Set an execution deadline in seconds from now."""
        if duration_s is not None:
            self.EXECUTION_DEADLINE = time.time() + duration_s
        else:
            self.EXECUTION_DEADLINE = None

    def is_deadline_exceeded(self) -> bool:
        """Return True if execution deadline has been reached."""
        if self.EXECUTION_DEADLINE is None:
            return False
        return time.time() >= self.EXECUTION_DEADLINE

    def get_time_remaining(self) -> float | None:
        """Return seconds remaining until deadline, or None if unlimited."""
        if self.EXECUTION_DEADLINE is None:
            return None
        return max(0.0, self.EXECUTION_DEADLINE - time.time())

    @contextlib.contextmanager
    def measure_stage(self, name: str, count: int = 0, units: str = "items"):
        """Context manager to measure the execution duration of a stage if profiling is active."""
        if getattr(self, "STAGE_PROFILER", None) is not None:
            self.STAGE_PROFILER.start_stage(name)
            stage = self.STAGE_PROFILER.stages.get(name)
            try:
                yield stage
            finally:
                final_count = stage.count if (stage is not None and stage.count > 0) else count
                self.STAGE_PROFILER.stop_stage(name, count=final_count, units=units)
        else:
            yield None

    def emergency_shutdown(self, number_error: int = 1) -> None:
        """Close the program cleanly by deleting the git files."""
        if self.BP_ON_SHUTDOWN:
            self.BP()

        gp_obj = getattr(sys.modules.get("__main__"), "gp", None)
        if gp_obj and hasattr(gp_obj, "PURGE_LIST"):
            for directory in gp_obj.PURGE_LIST:
                with contextlib.suppress(Exception):
                    shutil.rmtree(directory)
        with contextlib.suppress(Exception):
            from core.ssh_tunnel import stop_ssh_tunnel
            stop_ssh_tunnel()
        sys.exit(number_error)
        return

    @classmethod
    def BP(cls) -> None:  # noqa: N802
        """Breakpoint with instructions."""
        sys.breakpointhook()  # noqa: T100
        return

    def string_shortner(self, text: str, mode: str = "boxed+indent") -> str:
        """Beautify string."""
        formated_text = ""
        wrapped_text = wrap_lines(
            [text],
            self.OVERRIDE_MAX_PRINT_SIZE + (self.OVERRIDE_MAX_PRINT_SIZE / 2),
        )

        if mode == "boxed":
            formated_list = render_ansi_box(wrapped_text)
        elif mode == "indent":
            formated_list = render_with_indent(wrapped_text, "    > ")
        elif mode == "boxed+indent":
            formated_list = render_ansi_box(render_with_indent(wrapped_text, "    > "))

        for line in formated_list[0]:
            formated_text = f"{formated_text}\n{line}"
        return formated_text

    def is_PointerType(self, variable: PointerType) -> bool:
        if isinstance(variable, tuple) and len(variable) == 2:  # noqa: PLR2004, SIM102
            if isinstance(variable[0], int) and isinstance(variable[1], int):
                return True
        return False

    def is_JoinType(self, variable: JoinType) -> bool:
        if isinstance(variable, tuple):
            if len(variable) == 1:
                return self.is_PointerType(variable[0])

            if len(variable) == 3:  # noqa: PLR2004, SIM102
                if self.is_PointerType(variable[0]) and self.is_PointerType(variable[1]) and isinstance(variable[2], int):
                    return True
        return False

    def is_JoinsType(self, variable: JoinsType) -> bool:
        if isinstance(variable, tuple) and len(variable) != 0:
            return all(self.is_JoinType(possible_join) for possible_join in variable)
        return False

    def is_OperationType(self, variable: OperationType) -> bool:
        if isinstance(variable, tuple) and len(variable) == 3:  # noqa: PLR2004, SIM102
            if isinstance(variable[0], int) or self.is_JoinsType(variable[0]):  # noqa: SIM102
                if isinstance(variable[1], int) and isinstance(variable[2], tuple):
                    return True
        return False

    def is_LinkType(self, variable: LinkType) -> bool:
        return isinstance(variable, (int, str))

    def is_RouteType(self, variable: RouteType) -> bool:
        if isinstance(variable, (tuple, list)) and len(variable) != 0:
            return all(self.is_LinkType(possible_link) for possible_link in variable)
        return False

    def is_RefType(self, variable: RefType) -> bool:
        if isinstance(variable, tuple) and len(variable) == 3:  # noqa: PLR2004, SIM102
            if self.is_PointerType(variable[0]) and isinstance(variable[1], int) and self.is_RouteType(variable[2]):
                return True
        return False

    def is_SafeDataType(self, variable: SafeDataType) -> bool:
        return bool(isinstance(variable, (int, str, bytes)) or variable is None)

    def is_UnSafeDataType(self, variable: UnSafeDataType) -> bool:
        return bool(self.is_SafeDataType(variable) or self.is_RefType(variable))

    def OP_isinstance(self, arg: any, expected_type: any) -> bool:
        if (type_detected := self.OP_isinstanceDict.get(expected_type)) is not None:
            return type_detected(arg)
        else:
            return isinstance(arg, expected_type)

    def type_check(self, *expected_types):
        def decorator(func):
            if not self.DEBUG_TYPECHECK:
                return func

            @wraps(func)
            def wrapper(*args, **kwargs):
                try:
                    for i, expected_type in enumerate(expected_types):
                        if expected_type == Self:
                            continue
                        if isinstance(expected_type, set):
                            assert all(any(self.OP_isinstance(item, e_type) for e_type in expected_type) for item in args[i:]), f"Type Error expected:{expected_type}| Received: {args[i:]}"
                            raise IndexError  # noqa: TRY301
                        if isinstance(expected_type, tuple):
                            assert any(self.OP_isinstance(args[i], e_type) for e_type in expected_type), f"Type Error expected:{expected_type}| Received: {args[i]}"
                        else:
                            assert self.OP_isinstance(args[i], expected_type), f"Type Error expected:{expected_type}| Received: {args[i]}"
                except IndexError:
                    pass
                result = func(*args, **kwargs)
                return result
            return wrapper
        return decorator


G = GlobalStuff()


class COLOR:
    """Allow colors to be drawn, will concatenate color code to start/end."""

    @classmethod
    def green(cls, string_arg: str) -> str:
        """Green color codes."""
        return f"\033[92m{string_arg}\033[0m"

    @classmethod
    def red(cls, string_arg: str) -> str:
        """Red color codes."""
        return f"\033[91m{string_arg}\033[0m"

    @classmethod
    def yellow(cls, string_arg: str) -> str:
        """Yellow color codes."""
        return f"\033[93m{string_arg}\033[0m"

    @classmethod
    def magenta(cls, string_arg: str) -> str:
        """Magenta color codes."""
        return f"\033[35m{string_arg}\033[0m"

    @classmethod
    def cyan(cls, string_arg: str) -> str:
        """Cyan color codes."""
        return f"\033[36m{string_arg}\033[0m"


class PointerGetter:
    """Iterator that returns the pointer and repetition from a joins in order."""

    def __init__(self, joins: JoinsType | int) -> None:
        """Parse joins and detects single table joins."""
        self.joins = joins
        if not isinstance(joins, int):
            self.current = -1
            self.end = len(joins)
            if len(joins) == 1 and len(joins[0]) == 1:
                self.end = 0

    def __iter__(self) -> Self:  # noqa: D105
        return self

    def __next__(self) -> tuple[int, JoinType]:
        """Get next join."""
        if self.current >= self.end:
            raise StopIteration

        self.current += 1

        if self.current == 0:
            return (1, self.joins[0][0])
        return (
            self.joins[self.current - 1][2],
            self.joins[self.current - 1][1],
        )

    def get_first_pointer(self) -> PointerType:
        """Get the first pointer of the joins."""
        return self.joins[0][0]

    def get_first_table_id(self) -> int:
        """Get the first table_id of the joins."""
        if isinstance(self.joins, int):
            return self.joins
        return self.joins[0][0][0]

    @staticmethod
    def add_join(joins: list, join: tuple) -> None:
        """Add a join tuple to a joins list, upgrading initial single pointer or incrementing repeat count."""
        if len(joins[0]) == 1:
            if join[0] == joins[0][0]:
                joins[0] = (joins[0][0], join[1], join[2])
                return
            else:
                G.emergency_shutdown(52)

        if joins[-1][1] == join[1]:
            joins[-1] = (joins[-1][0], joins[-1][1], joins[-1][2] + 1)
            return

        joins.append(join)


_RE_C_DECLARATION = re.compile(
    r"^\s*(#\s*include|typedef|struct\s+\w+\s*\{|union\s+\w+\s*\{|enum\s+\w+\s*\{|extern|static|inline|__inline__|void|int|char|long)\b",
    re.MULTILINE,
)
_RE_ASM_PIPE_COMMENT = re.compile(r"^\s*\|(\s+[a-zA-Z0-9_-]|\s*$)", re.MULTILINE)
_RE_ASM_DIRECTIVE = re.compile(
    r"^\s*\.(set|globl|global|macro|endm|section|equ|equiv|ascii|asciz|byte|short|long|quad|word|space|align|balign|fill|text|data|bss)\s+[a-zA-Z0-9_.]+",
    re.MULTILINE,
)


def is_pure_asm_content(content: str) -> bool:
    """Detect whether a C or header source file is entirely architecture assembly."""
    if not content:
        return False
    if _RE_C_DECLARATION.search(content):
        return False
    return bool(_RE_ASM_PIPE_COMMENT.search(content) or _RE_ASM_DIRECTIVE.search(content))


def type_check(name: str, content: str | None = None) -> int:
    """Parse string to get file type."""
    if name.endswith((".c", ".h")):
        if content is not None and is_pure_asm_content(content):
            return T_ASM
        return T_C
    if name == "MAINTAINERS" or name.endswith("/MAINTAINERS"):
        return T_MAINTAINERS
    if name == "CREDITS" or name.endswith("/CREDITS"):
        return T_CREDITS
    if name.endswith((".S", ".s")):
        return T_ASM
    if name.endswith("Kconfig"):
        return T_KCONFIG
    if name.endswith(".rs"):
        return T_RUST
    return T_RAW


class ASTT(IntEnum):
    Undefined = 0
    # C
    C_Compound = auto()
    C_Comment = auto()
    C_Keyword = auto()
    ## Storage Class
    C_SCauto = auto()
    C_SCregister = auto()
    C_SCstatic = auto()
    C_SCextern = auto()
    C_SC_Thread_local = auto()
    C_SCthread_local = auto()
    C_SCtypedef = auto()
    C_SCconstexpr = auto()
    ## Function Specifiers
    C_FSinline = auto()
    C_FS_Noreturn = auto()
    ## Alignment Specifiers
    C_AS__Alignas = auto()
    C_AS_alignas = auto()
    ## Qualifiers
    C_Qconst = auto()
    C_Qvolatile = auto()
    C_Qrestrict = auto()
    C_Q_Atomic = auto()
    ## Qualifiers End
    C_functionproto = auto()
    C_functionprotodecl = auto()
    C_functionprotnotbind = auto()
    C_array = auto()
    C_arrayempty = auto()
    C_enumequal = auto()
    C_pointer = auto()
    C_struct = auto()
    C_structdecl = auto()
    C_structnotbind = auto()
    C_enum = auto()
    C_enumdecl = auto()
    C_enumnotbind = auto()
    C_union = auto()
    C_uniondecl = auto()
    C_unionnotbind = auto()
    C_void = auto()
    C_unsigned = auto()
    C_signed = auto()
    C_char = auto()
    C_short = auto()
    C_int = auto()
    C_long = auto()
    C_bool = auto()
    C_float = auto()
    C_double = auto()
    ## C Statements & Expressions
    C_CompoundStmt = auto()
    C_IfStmt = auto()
    C_SwitchStmt = auto()
    C_CaseStmt = auto()
    C_DefaultStmt = auto()
    C_WhileStmt = auto()
    C_DoStmt = auto()
    C_ForStmt = auto()
    C_ReturnStmt = auto()
    C_BreakStmt = auto()
    C_ContinueStmt = auto()
    C_GotoStmt = auto()
    C_LabelStmt = auto()
    C_AsmStmt = auto()
    C_CallExpr = auto()
    C_MemberRefExpr = auto()
    C_DeclRefExpr = auto()
    C_BinaryOperator = auto()
    C_UnaryOperator = auto()
    C_ParenExpr = auto()
    C_InitListExpr = auto()
    ## CPPro
    CPPro_if = auto()
    CPPro_elif = auto()
    CPPro_else = auto()
    CPPro_endif = auto()
    CPPro_ifdef = auto()
    CPPro_ifndef = auto()
    CPPro_elifdef = auto()
    CPPro_elifndef = auto()
    CPPro_define = auto()
    CPPro_define_macro = auto()
    CPPro_undef = auto()
    CPPro_include = auto()
    CPPro_line = auto()
    CPPro_error = auto()
    CPPro_warning = auto()
    CPPro_pragma = auto()
    ## ASM
    ASM_Macro = auto()
    ASM_Directive = auto()
    ASM_Instruction = auto()
    ASM_Label = auto()
    ASM_Comment = auto()
    ## Kconfig Grammar Constructs
    Kconfig_Mainmenu = auto()
    Kconfig_Menu = auto()
    Kconfig_Endmenu = auto()
    Kconfig_Config = auto()
    Kconfig_Menuconfig = auto()
    Kconfig_Choice = auto()
    Kconfig_Endchoice = auto()
    Kconfig_Comment = auto()
    Kconfig_Source = auto()
    Kconfig_Rsource = auto()
    Kconfig_If = auto()
    Kconfig_Endif = auto()
    ## Kconfig Types & Properties
    Kconfig_Type_Bool = auto()
    Kconfig_Type_Tristate = auto()
    Kconfig_Type_String = auto()
    Kconfig_Type_Hex = auto()
    Kconfig_Type_Int = auto()
    Kconfig_Prompt = auto()
    Kconfig_Default = auto()
    Kconfig_Depends_On = auto()
    Kconfig_Select = auto()
    Kconfig_Imply = auto()
    Kconfig_Range = auto()
    Kconfig_Help = auto()
    ## Kconfig Logical Expressions
    Kconfig_Op_And = auto()
    Kconfig_Op_Or = auto()
    Kconfig_Op_Not = auto()
    Kconfig_Op_Equal = auto()
    Kconfig_Op_Unequal = auto()
    Kconfig_Symbol_Ref = auto()
    Kconfig_Constant = auto()
    ## Maintainer Constructs
    Maintainer_Section = auto()
    Maintainer_Member = auto()
    Maintainer_Pattern = auto()
    Credits_Entry = auto()
    ## Rust Constructs
    Rust_Struct = auto()
    Rust_Field = auto()
    Rust_Enum = auto()
    Rust_Variant = auto()
    Rust_Union = auto()
    Rust_Fn = auto()
    Rust_Param = auto()
    Rust_Trait = auto()
    Rust_TraitAlias = auto()
    Rust_Impl = auto()
    Rust_Type = auto()
    Rust_Const = auto()
    Rust_Static = auto()
    Rust_Mod = auto()
    Rust_Use = auto()
    Rust_MacroDef = auto()
    Rust_MacroCall = auto()
    Rust_ForeignMod = auto()
    Rust_Attribute = auto()
    Rust_DocComment = auto()
    Rust_Comment = auto()
    ## Fallback / Raw Content Construct
    Raw_Content = auto()
    ## Additional C Constructs
    C_SizeofExpr = auto()
    C_TypeRef = auto()
    C_SyscallDef = auto()
    C_AsmEntry = auto()
    C_ModuleAttr = auto()
    C_MacroVarDef = auto()
    C_TracepointDef = auto()
    C_PerCpuVarDef = auto()
    C_ModuleParamDef = auto()
    C_CastExpr = auto()
    C_ConditionalOperator = auto()
    C_ArraySubscriptExpr = auto()
    C_DesignatedInitExpr = auto()
    C_CompoundAssignOperator = auto()
    C_CompoundLiteralExpr = auto()
    C_IndirectGotoStmt = auto()
    C_AddrLabelExpr = auto()
    C_NullStmt = auto()
    C_InitcallDef = auto()
    C_ExportSymbolDef = auto()
    CPPro_embed = auto()
    CPPro_defined = auto()
    C_StmtExpr = auto()
    C_GenericSelectionExpr = auto()
    C_VaArgExpr = auto()
    C_LabelRef = auto()
    C_IntegerLiteral = auto()
    C_FloatingLiteral = auto()
    C_StringLiteral = auto()
    C_CharacterLiteral = auto()
    C_DeviceTableDef = auto()
    C_SysfsAttrDef = auto()
    C_MacroParam = auto()
    ## Maintainer Taxonomy
    Maintainer_Person = auto()
    Maintainer_List = auto()
    Maintainer_Web = auto()
    Maintainer_Tree = auto()
    Maintainer_Status = auto()
    ## C Basic Types
    C_longlong = auto()
    C_int128 = auto()
    C_complex = auto()
    ## GNU C & C11 Expressions
    C_StaticAssert = auto()
    C_TypeofExpr = auto()
    C_OffsetofExpr = auto()
    C_TypesCompatiblePExpr = auto()
    C_ChooseExpr = auto()
    ## Kconfig Additional Constructs
    Kconfig_Osource = auto()
    Kconfig_Def_Bool = auto()
    Kconfig_Def_Tristate = auto()
    Kconfig_Visible = auto()
    ## Alignment & Atomic Operations
    C_AlignofExpr = auto()
    C_AtomicExpr = auto()
    ## GNU Preprocessor Extensions
    CPPro_include_next = auto()
    CPPro_ident = auto()
    ## Maintainer Extended Taxonomy
    Maintainer_Bug = auto()
    Maintainer_Chat = auto()
    Maintainer_Patchwork = auto()
    ## Extended C Width, Floating-Point, Vector & Bitfield Types
    C_Bitfield = auto()
    C_char16 = auto()
    C_char32 = auto()
    C_wchar = auto()
    C_longdouble = auto()
    C_float128 = auto()
    C_float16 = auto()
    C_Vector = auto()
    C_vla = auto()


class SymbolLinkage(IntEnum):
    ExternalDef = 0       # Global non-static definition
    InternalStaticDef = 1 # File-local static definition in a .c file
    PrototypeDecl = 2     # Forward declaration or header prototype without body
    InlineHeaderDef = 3   # static inline or extern inline definition in a header (.h)
    WeakOrAliasDef = 4    # __weak or __alias(...) definition
    MacroOrKernelDef = 5  # Preprocessor #define, SYSCALL_DEFINE*, TRACE_EVENT, DEFINE_SPINLOCK, EXPORT_SYMBOL


class SymbolRole(IntEnum):
    Declaration = 1
    TypeUsage = 2
    Call = 3
    MemberRef = 4
    DeclRef = 5
    MacroExpansion = 6


class FileRefType(IntEnum):
    Include = 1        # C/ASM #include <header.h>
    Kconfig = 2        # Kconfig source/rsource "path"
    Kbuild = 3         # Kbuild compilation (obj-* += foo.o -> foo.c)
    Makefile = 4       # Makefile include (include path) or recursion (obj-* += dir/)
    Documentation = 5  # Documentation / text reference (Documentation/, *.txt, *.rst, README)


FILE_REF_TYPE_LABELS: dict[int, str] = {
    FileRefType.Include: "Include",
    FileRefType.Kconfig: "Kconfig",
    FileRefType.Kbuild: "Kbuild",
    FileRefType.Makefile: "Makefile",
    FileRefType.Documentation: "Documentation",
}


def format_ref_type_label(ref_type: int) -> str:
    """Return friendly label for a FileRefType code."""
    return FILE_REF_TYPE_LABELS.get(ref_type, f"RefType({ref_type})")


STANDARD_C_KEYWORDS: dict[str, ASTT] = {
    "if": ASTT.C_IfStmt,
    "switch": ASTT.C_SwitchStmt,
    "case": ASTT.C_CaseStmt,
    "default": ASTT.C_DefaultStmt,
    "while": ASTT.C_WhileStmt,
    "do": ASTT.C_DoStmt,
    "for": ASTT.C_ForStmt,
    "return": ASTT.C_ReturnStmt,
    "break": ASTT.C_BreakStmt,
    "continue": ASTT.C_ContinueStmt,
    "goto": ASTT.C_GotoStmt,
    "asm": ASTT.C_AsmStmt,
    "__asm__": ASTT.C_AsmStmt,
    "__asm": ASTT.C_AsmStmt,
    "sizeof": ASTT.C_SizeofExpr,
    "alignof": ASTT.C_AlignofExpr,
    "_Alignof": ASTT.C_AlignofExpr,
    "__alignof__": ASTT.C_AlignofExpr,
    "__alignof": ASTT.C_AlignofExpr,
    "alignas": ASTT.C_AS_alignas,
    "_Alignas": ASTT.C_AS__Alignas,
    "auto": ASTT.C_SCauto,
    "register": ASTT.C_SCregister,
    "static": ASTT.C_SCstatic,
    "extern": ASTT.C_SCextern,
    "typedef": ASTT.C_SCtypedef,
    "const": ASTT.C_Qconst,
    "volatile": ASTT.C_Qvolatile,
    "restrict": ASTT.C_Qrestrict,
    "_Atomic": ASTT.C_Q_Atomic,
    "inline": ASTT.C_FSinline,
    "__inline__": ASTT.C_FSinline,
    "__inline": ASTT.C_FSinline,
    "void": ASTT.C_void,
    "char": ASTT.C_char,
    "short": ASTT.C_short,
    "int": ASTT.C_int,
    "long": ASTT.C_long,
    "signed": ASTT.C_signed,
    "unsigned": ASTT.C_unsigned,
    "float": ASTT.C_float,
    "double": ASTT.C_double,
    "struct": ASTT.C_struct,
    "union": ASTT.C_union,
    "enum": ASTT.C_enum,
    "bool": ASTT.C_bool,
    "_Bool": ASTT.C_bool,
    "typeof": ASTT.C_TypeofExpr,
    "__typeof__": ASTT.C_TypeofExpr,
    "__typeof": ASTT.C_TypeofExpr,
    "static_assert": ASTT.C_StaticAssert,
    "_Static_assert": ASTT.C_StaticAssert,
    "_Float128": ASTT.C_float128,
    "__float128": ASTT.C_float128,
    "_Float16": ASTT.C_float16,
    "__fp16": ASTT.C_float16,
    "char16_t": ASTT.C_char16,
    "char32_t": ASTT.C_char32,
    "wchar_t": ASTT.C_wchar,
    "__int128": ASTT.C_int128,
    "__int128_t": ASTT.C_int128,
    "__uint128_t": ASTT.C_int128,
    "_Complex": ASTT.C_complex,
}



class FileContextFormatter(logging.Formatter):
    """Logging formatter that prepends [current_file] if G.CURRENT_PARSING_FILE is set."""

    def format(self, record: logging.LogRecord) -> str:
        file_ctx = getattr(G, "CURRENT_PARSING_FILE", None)
        if file_ctx and not str(record.msg).startswith(f"[{file_ctx}]"):
            record.msg = f"[{file_ctx}] {record.msg}"
        return super().format(record)


def configure_logging(fmt: str = "%(asctime)s - %(levelname)s - %(message)s", level: int = logging.INFO) -> None:
    """Configure root logger and ensure all handlers use FileContextFormatter."""
    if not logging.root.handlers:
        logging.basicConfig(level=level, format=fmt)

    for handler in logging.root.handlers:
        h_fmt = handler.formatter._fmt if handler.formatter and getattr(handler.formatter, "_fmt", None) else fmt
        handler.setFormatter(FileContextFormatter(h_fmt))


configure_logging()


_MEMORY_GUARD_INITIALIZED: bool = False


def setup_memory_limit(limit_gb: float = 60.0) -> None:
    """Enforce a 60GB boundary via Linux cgroups v2 (memory.max, memory.high, memory.oom.group)
    with graceful fallback to per-process address space limit (RLIMIT_AS)."""
    global _MEMORY_GUARD_INITIALIZED
    if _MEMORY_GUARD_INITIALIZED:
        return
    if not sys.platform.startswith("linux"):
        return

    limit_bytes = int(limit_gb * 1024 * 1024 * 1024)
    high_bytes = int(limit_gb * 0.9 * 1024 * 1024 * 1024)
    logger = logging.getLogger("core.globalstuff")

    # 1. Check if process is already running inside an active cgroups v2 memory-constrained scope
    try:
        cg_path = None
        if os.path.exists("/proc/self/cgroup"):
            with open("/proc/self/cgroup", "r") as f:
                for line in f:
                    parts = line.strip().split(":")
                    if len(parts) >= 3 and (parts[0] == "0" or not parts[1]):
                        cg_path = parts[2]
                        break

        if cg_path:
            full_cg_dir = f"/sys/fs/cgroup{cg_path}"
            mem_max_file = os.path.join(full_cg_dir, "memory.max")
            if os.path.isfile(mem_max_file):
                with open(mem_max_file, "r") as f:
                    val = f.read().strip()
                if val != "max" and int(val) <= limit_bytes:
                    # Already constrained by cgroups v2
                    oom_group_file = os.path.join(full_cg_dir, "memory.oom.group")
                    if os.path.isfile(oom_group_file) and os.access(oom_group_file, os.W_OK):
                        with open(oom_group_file, "w") as f:
                            f.write("1")
                    mem_high_file = os.path.join(full_cg_dir, "memory.high")
                    if os.path.isfile(mem_high_file) and os.access(mem_high_file, os.W_OK):
                        with open(mem_high_file, "w") as f:
                            f.write(str(high_bytes))

                    _MEMORY_GUARD_INITIALIZED = True
                    logger.info(
                        COLOR.green(
                            f"[Memory Guard] Active cgroups v2 boundary verified: "
                            f"max={int(val) / (1024**3):.1f} GB, high={high_bytes / (1024**3):.1f} GB, oom.group=1 ({cg_path})"
                        )
                    )
                    return
    except Exception as e:
        logger.debug(f"[Memory Guard] cgroups inspection check skipped: {e}")

    # 2. If unconstrained and systemd-run is available, re-exec inside a dedicated cgroup scope (except in unit tests)
    is_unit_test = any(arg in sys.argv for arg in ("-u", "--unit-test")) or "unittest" in sys.modules or "pytest" in sys.modules
    if not is_unit_test and os.environ.get("KERNELINFO_CGROUP_ACTIVE") != "1" and shutil.which("systemd-run"):
        try:
            os.environ["KERNELINFO_CGROUP_ACTIVE"] = "1"
            logger.info(COLOR.cyan(f"[Memory Guard] Activating cgroups v2 scope via systemd-run: max={limit_gb:.1f} GB, high={limit_gb*0.9:.1f} GB..."))
            os.execvp(
                "systemd-run",
                [
                    "systemd-run",
                    "--user",
                    "--scope",
                    "-p",
                    f"MemoryMax={int(limit_gb)}G",
                    "-p",
                    f"MemoryHigh={int(limit_gb * 0.9)}G",
                    sys.executable,
                ]
                + sys.argv,
            )
        except Exception as e:
            logger.warning(COLOR.yellow(f"[Memory Guard] systemd-run scope activation failed: {e}. Falling back to RLIMIT_AS."))

    # 3. Fallback: enforce RLIMIT_AS address space limit
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        if hard != resource.RLIM_INFINITY:
            limit_bytes = min(limit_bytes, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, hard))
        _MEMORY_GUARD_INITIALIZED = True
        logger.info(
            COLOR.green(f"[Memory Guard] Hard address space limit set to {limit_bytes / (1024**3):.1f} GB (RLIMIT_AS)")
        )
    except Exception as e:
        logger.warning(
            COLOR.yellow(f"[Memory Guard] Could not configure RLIMIT_AS: {e}")
        )


try:
    import ctypes
    _libc = ctypes.CDLL("libc.so.6")
    _malloc_trim = _libc.malloc_trim
    _malloc_trim.argtypes = [ctypes.c_size_t]
    _malloc_trim.restype = ctypes.c_int
except Exception:
    _malloc_trim = None


def reclaim_system_memory() -> None:
    """Force CPython garbage collection and release heap pages back to OS via glibc malloc_trim."""
    import gc
    gc.collect()
    if _malloc_trim is not None:
        try:
            _malloc_trim(0)
        except Exception:
            pass


def freeze_system_memory() -> None:
    """Reclaim unreferenced memory and freeze living objects into permanent GC generation for CoW safety."""
    import gc
    reclaim_system_memory()
    if hasattr(gc, "freeze"):
        try:
            gc.freeze()
        except Exception:
            pass


def unfreeze_system_memory() -> None:
    """Unfreeze permanent GC generation back into normal generational tracking."""
    import gc
    if hasattr(gc, "unfreeze"):
        try:
            gc.unfreeze()
        except Exception:
            pass



