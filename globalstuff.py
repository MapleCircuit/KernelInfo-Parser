"""globalstuff.py - Global objects and constants."""
import sys
import shutil
from pathlib import Path
from StringWrangler import wrap_lines, render_ansi_box, render_with_indent
import contextlib
from typing import Self
from functools import wraps


OP_DONE, OP_SET, OP_UPDATE, OP_REF, OP_VIEW_DONE, OP_VIEW_SET = range(6)
REF_ROOT, REF_OLD, REF_POS, REF_FILE, REF_C_AST = range(5)
T_DIR, T_C, T_KCONFIG, T_RUST = range(4)

PointerType = tuple[int, int]
JoinType = tuple[PointerType, PointerType, int] | tuple[PointerType]
JoinsType = tuple[JoinType, ...]

OperationType = tuple[JoinsType | int, int, tuple]

LinkType = int | str
RouteType = tuple[LinkType, ...] | list[LinkType, ...]
RefType = tuple[PointerType, int, RouteType]

SafeDataType = int|str|None
UnSafeDataType = SafeDataType|RefType





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
        self.RAMDISK = "/dev/shm"  # noqa: S108
        self.CPUS = 8
        self.linux_directory = Path("linux")

        self.CLEAN_PRINT = True

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
        self.DEBUG_TYPECHECK = True

        self.OP_isinstanceDict = {
            PointerType:self.is_PointerType,
            JoinType:self.is_JoinType,
            JoinsType:self.is_JoinsType,
            OperationType:self.is_OperationType,
            LinkType:self.is_LinkType,
            RouteType:self.is_RouteType,
            RefType:self.is_RefType,
            SafeDataType:self.is_SafeDataType,
            UnSafeDataType:self.is_UnSafeDataType,
            None:lambda x: x is None,
        }



    @classmethod
    def emergency_shutdown(cls, number_error: int=1) -> None:
        """Close the program cleanly by deleting the git files."""
        for directory in sys.modules["__main__"].gp.PURGE_LIST:
            with contextlib.suppress(Exception):
                shutil.rmtree(directory)
        sys.exit(number_error)
        return

    @classmethod
    def BP(cls) -> None:  # noqa: N802
        """Breakpoint with instructions."""
        print("""
=====BREAKPOINT=====
c: Continue execution
q: Quit the debugger
n: Step to the next line within the same function
s: Step to the next line in this function or a called function.
""")
        sys.breakpointhook()  # noqa: T100
        return

    def string_shortner(self, text: str, mode: str="boxed+indent") -> str:
        """Butify my string."""
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
        if isinstance(variable, tuple) and len(variable)==2:  # noqa: PLR2004, SIM102
            if isinstance(variable[0], int) and isinstance(variable[1], int):
                return True
        return False

    def is_JoinType(self, variable: JoinType) -> bool:
        if isinstance(variable, tuple):
            if len(variable)==1:
                return self.is_PointerType(variable[0])

            if len(variable)==3:  # noqa: PLR2004, SIM102
                if self.is_PointerType(variable[0]) and self.is_PointerType(variable[1]) and isinstance(variable[2], int):
                    return True
        return False

    def is_JoinsType(self, variable: JoinsType) -> bool:
        if isinstance(variable, tuple) and len(variable)!=0:
            return all(self.is_JoinType(possible_join) for possible_join in variable)
        return False

    def is_OperationType(self, variable: OperationType) -> bool:
        if isinstance(variable, tuple) and len(variable)==3:  # noqa: PLR2004, SIM102
            if isinstance(variable[0], int) or self.is_JoinsType(variable[0]):  # noqa: SIM102
                if isinstance(variable[1], int) and isinstance(variable[2], tuple):
                    return True
        return False

    def is_LinkType(self, variable: LinkType) -> bool:
        return isinstance(variable, (int, str))

    def is_RouteType(self, variable: RouteType) -> bool:
        if isinstance(variable, (tuple, list)) and len(variable)!=0:
            return all(self.is_LinkType(possible_link) for possible_link in variable)
        return False

    def is_RefType(self, variable: RefType) -> bool:
        if isinstance(variable, tuple) and len(variable)==3:  # noqa: PLR2004, SIM102
            if self.is_PointerType(variable[0]) and isinstance(variable[1], int) and self.is_RouteType(variable[2]):
                return True
        return False

    def is_SafeDataType(self, variable: SafeDataType) -> bool:
        return bool(isinstance(variable, (int, str)) or variable is None)

    def is_UnSafeDataType(self, variable: UnSafeDataType) -> bool:
        return bool(self.is_SafeDataType(variable) or self.is_RefType(variable))



    def OP_isinstance(self, arg: any, expected_type: any) -> bool:
        #print(f"OP_isinstance arg:{arg} expected_type:{expected_type}")

        if (type_detected := self.OP_isinstanceDict.get(expected_type)) is not None:
            return type_detected(arg)
        else:
            return isinstance(arg, expected_type)


    def type_check(self, *expected_types):
        def decorator(func):
            @wraps(func)
            def wrapper(*args, **kwargs):
                if self.DEBUG_TYPECHECK:
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
                # before exec
                result = func(*args, **kwargs)
                # after exec
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
    """Iterator that returns the pointer and repetition from a joins in order.

    Here is a good base for its use:
    for repeat, pointer in PointerGetter(joins):
        for x in range(repeat):
    """

    def __init__(self, joins: JoinsType|int) -> None:
        """Parse joins and detects single table joins.

        Will also accept table_id as an arg, to be used with get_first_pointer.
        """
        self.joins = joins
        if not isinstance(joins, int):
            self.current = -1
            self.end = len(joins)
            # check for view size 1 without 2nd pointer
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
            return (1, self.joins[0][0])  # Get the first pointer
        return (
            self.joins[self.current - 1][2],
            self.joins[self.current - 1][1],
        )  # Get subsequent pointer

    def get_first_pointer(self) -> PointerType:
        """Get the first pointer of the joins."""
        if isinstance(self.joins, int):
            return self.joins
        return self.joins[0][0]

    def get_first_table_id(self) -> int:
        """Get the first table_id of the joins."""
        return self.joins[0][0][0]

def type_check(name: str) -> int | None:
    """Parse string to get file type."""
    if name.endswith((".c", ".h")):
        return T_C
    if name.endswith("Kconfig"):
        return T_KCONFIG
    if name.endswith(".h"):
        return T_RUST
    return None
