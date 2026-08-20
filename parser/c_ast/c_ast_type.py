from core.globalstuff import G, COLOR, REF_POS, REF_ROOT, REF_OLD, REF_MULTI, REF_NO_REF, ASTT, IntEnum, Flag, auto, RefType, OP_REF, RouteType
import clang.cindex as cc
import logging
import json
from typing import Self
import random

logger = logging.getLogger(__name__)

# Linter bypass
m_v_main = m_file_name = m_file = m_bridge_file = m_moved_file = m_type_descriptor = m_ast = m_ast_container = m_ast_include = m_ast_debug = m_tag = m_bridge_tag = None
ChangeSetType = None

def serializer(obj: object) -> dict:
    """For ast_debug."""
    return obj.__dict__

def good_looking_printing(object_name: str, pre_result: str="", post_result: str=" ") -> str:
    """Print AST without headache."""
    result = " "
    multi_line_leap = False
    list_wait_arr = []
    for key in vars(object_name):
        if not getattr(object_name, key):
            continue
        if isinstance(getattr(object_name, key), (list, tuple)):
            list_wait_arr.append(key)
        else:
            to_be_added = f"{COLOR.magenta(key)}:{getattr(object_name, key)},"
            if len(result.splitlines()[-1]) > G.OVERRIDE_MAX_PRINT_SIZE:
                if not multi_line_leap:
                    pre_result += "\n"
                    result = f"   {result}"
                    multi_line_leap = True
                to_be_added = f"\n{to_be_added}"
            result += to_be_added
    result = result[:-1]  # comma remover

    if multi_line_leap:
        result = result.replace("\n", "\n   ")

    for key in list_wait_arr:
        # if multi_line_leap:
        # result += "   "
        result += f", {COLOR.green(key)}" + COLOR.green(": {")
        for key_key in getattr(object_name, key):
            result += "\n   "
            result += str(key_key).replace("\n", "\n   ")
        result += COLOR.green("\n}")
    return f"{pre_result}{result[1:]}{post_result}"



# This applies to c_ast and c_ast_type only
# =========================================
# The 














class Line:
    """Represent position of code, can extract the underlying str."""

    def __init__(self, *args: int|object) -> None:
        """Init the line pos and optionaly the col pos, accept cc.SourceRange."""
        self.code = ""
        match len(args):
            case 0:
                self.line_pos = (0, 0)
                self.char_pos = (0, 0)
            case 1:
                if isinstance(args[0], cc.SourceRange):
                    self.line_pos = (args[0].start.line, args[0].end.line)
                    self.char_pos = (args[0].start.column, args[0].end.column)
                elif isinstance(args[0], Line):
                    self.line_pos = (args[0].line_pos[0], args[0].line_pos[1])
                    self.char_pos = (args[0].char_pos[0], args[0].char_pos[1])
                else:
                    logger.error("Line: 1 ARGS TYPE ERROR")
            case 2:
                self.line_pos = (args[0], args[1])
                self.char_pos = (0, 0)
            case 4:
                self.line_pos = (args[0], args[1])
                self.char_pos = (args[2], args[3])

    # Code Capture
    def cc(self, rawfile: tuple[str]) -> Self:
        """Extract the str using line/col pos."""

        # Line Select
        try:
            if self.line_pos[0] == self.line_pos[1]:
                self.code = rawfile[self.line_pos[0] - 1]
            else:
                self.code = "\n".join(rawfile[self.line_pos[0] - 1 : self.line_pos[1]])
        except IndexError:
            self.code = ""
            return self

        char_start = 0 if self.char_pos[0] == 0 else self.char_pos[0] - 1

        if self.char_pos[1] == 0:
            self.code = self.code[char_start:]
        else:
            char_end = self.char_pos[1] - 1

            #  Char Trim
            try:
                self.code = self.code[
                    char_start : (char_end - len(rawfile[self.line_pos[1] - 1]))
                ]
            except IndexError:
                self.code = ""
        return self

    def new_end(self, *args: int|object) -> None:
        """Update the end values of Line, accept cc.SourceRange, cc.Token and Line."""
        if self.line_pos[0] == 0:
            self.__init__(*args)
            return

        match len(args):
            case 1:
                arg = args[0]
                if hasattr(arg, 'line'):
                    self.line_pos = (self.line_pos[0], arg.line.line_pos[1])
                    self.char_pos = (self.char_pos[0], arg.line.char_pos[1])
                elif isinstance(arg, Line):
                    self.line_pos = (self.line_pos[0], arg.line_pos[1])
                    self.char_pos = (self.char_pos[0], arg.char_pos[1])
                elif isinstance(arg, cc.SourceRange):
                    self.line_pos = (self.line_pos[0], arg.end.line)
                    self.char_pos = (self.char_pos[0], arg.end.column)
                else:
                    self.line_pos = (self.line_pos[0], arg)
            case 2:
                self.line_pos = (self.line_pos[0], args[0])
                self.char_pos = (self.char_pos[0], args[1])

    def new_end_reversed(self, *args: int|object) -> None:
        """Update the end values of Line, will use start vals, accept cc.SourceRange and Line."""
        if self.line_pos[0] == 0:
            self.__init__(*args)
            return

        match len(args):
            case 1:
                if isinstance(args[0], cc.SourceRange):
                    self.line_pos = (self.line_pos[0], args[0].start.line)
                    self.char_pos = (self.char_pos[0], args[0].start.column)
                elif isinstance(args[0], Line):
                    self.line_pos = (self.line_pos[0], args[0].line_pos[0])
                    self.char_pos = (self.char_pos[0], args[0].char_pos[0])
                else:
                    self.line_pos = (self.line_pos[0], args[0])
            case 2:
                self.line_pos = (self.line_pos[0], args[0])
                self.char_pos = (self.char_pos[0], args[1])

    def grow(self, *args: int|object) -> None:
        """Update the start and end values of Line, will use start vals, accept cc.SourceRange and Line."""
        if self.line_pos[0] == 0:
            self.__init__(*args)
            return


        if isinstance(args[0], cc.SourceRange):
            if self.line_pos[0] > args[0].start.line:
                self.line_pos = (args[0].start.line, self.line_pos[1])
                self.char_pos = (args[0].start.column, self.char_pos[1])
            elif self.line_pos[0] == args[0].start.line:
                if self.char_pos[0] > args[0].start.column:
                    self.char_pos = (args[0].start.column, self.char_pos[1])

            if self.line_pos[1] < args[0].end.line:
                self.line_pos = (self.line_pos[0], args[0].end.line)
                self.char_pos = (self.char_pos[0], args[0].end.column)
            elif self.line_pos[1] == args[0].end.line:
                if self.char_pos[1] < args[0].end.column:
                    self.char_pos = (self.char_pos[0], args[0].end.column)

        elif isinstance(args[0], Line):
            if self.line_pos[0] > args[0].line_pos[0]:
                self.line_pos = (args[0].line_pos[0], self.line_pos[1])
                self.char_pos = (args[0].char_pos[0], self.char_pos[1])
            elif self.line_pos[0] == args[0].line_pos[0]:
                if self.char_pos[0] > args[0].char_pos[0]:
                    self.char_pos = (args[0].char_pos[0], self.char_pos[1])

            if self.line_pos[1] < args[0].line_pos[1]:
                self.line_pos = (self.line_pos[0], args[0].line_pos[1])
                self.char_pos = (self.char_pos[0], args[0].char_pos[1])
            elif self.line_pos[1] == args[0].line_pos[1]:
                if self.char_pos[1] < args[0].char_pos[1]:
                    self.char_pos = (self.char_pos[0], args[0].char_pos[1])


    def is_inside(self, extent):
        """Test whether an extent/Line is within current Line."""
        if isinstance(extent, cc.SourceRange):
            extent = Line(extent)

        # line_pos squarely inside or outside.
        if (self.line_pos[0] > extent.line_pos[0]) or (extent.line_pos[1] > self.line_pos[1]):
            return False
        elif (self.line_pos[0] < extent.line_pos[0]) and (extent.line_pos[1] < self.line_pos[1]):
            return True

        # check char_pos if we are outside.
        if (self.line_pos[0] == extent.line_pos[0]) and (self.char_pos[0] > extent.char_pos[0]):
            return False
        if (self.line_pos[1] == extent.line_pos[1]) and (self.char_pos[1] < extent.char_pos[1]):
            return False

        return True


    def __str__(self) -> str:
        """Line to str with empty detection."""
        if (self.line_pos == (0, 0)) and (self.char_pos == (0, 0)):
            return "None"

        if G.OVERRIDE_C_AST_LINE_PRINT and "code" in vars(self):
            return f"(S{self.line_pos[0]}[{self.char_pos[0]}], E{self.line_pos[1]}[{self.char_pos[1]}], C­<{self.code}>)"

        return f"(S{self.line_pos[0]}[{self.char_pos[0]}], E{self.line_pos[1]}[{self.char_pos[1]}])"


def safe_spelling(token) -> str:
    """Safely return token.spelling, checking cached spelling_str and caching in-place."""
    if (cached := getattr(token, "spelling_str", None)) is not None:
        return cached
    try:
        spelling = token.spelling
    except (UnicodeDecodeError, Exception):
        spelling = ""
    try:
        token.spelling_str = spelling
    except Exception:
        pass
    return spelling


def safe_cursor_spelling(cursor) -> str:
    """Safely return cursor.spelling, caching in-place on cursor._spelling_str."""
    if (cached := getattr(cursor, "_spelling_str", None)) is not None:
        return cached
    try:
        spelling = cursor.spelling or ""
    except (UnicodeDecodeError, Exception):
        spelling = ""
    try:
        cursor._spelling_str = spelling
    except Exception:
        pass
    return spelling



class Ast:
    """Building block of parser.

    Allow to push to tables using extract.
    """

    def ast_debug(self, CS: ChangeSetType, ast_id_route: RouteType) -> None:
        """Add CS infos to m_ast_debug."""
        CS.store(m_ast_debug.set(
            CS.ref(m_ast.ast_id, *ast_id_route),
            json.dumps(self.__dict__, default=serializer),
        ))
        return

    def tag(self, CS: ChangeSetType, ast_id_route: RouteType, extent: Line|None=None) -> None:
        """Create or recycle tag for current AST."""
        self.extent = extent
        if self.extent is None:
            self.extent = Line(0, 0)
        else:
            self.extent.cc(CS.parsers["C_AM"].rawfile)

        current_tag = (
            None,
            CS.gp.VID,
            0,
            self.extent.code,
            CS.ref(m_ast.ast_id, *ast_id_route),
            0,
            0,
        )

        if CS.prior_tags and (self.extent.code != ""):
            for x, tag in enumerate(CS.prior_tags):
                # If tag found in prior_tags, set bridge and return
                if tag[6:] == current_tag[2:]:
                    with CS(REF_OLD):
                        CS.active_tag_list.append(x)
                        CS.store(m_bridge_tag.set(
                            CS.ref(m_file.fid, REF_ROOT, REF_OLD),
                            tag[4],
                            self.extent.line_pos[0],
                            self.extent.line_pos[1],
                            self.extent.char_pos[0],
                            self.extent.char_pos[1],
                        ))
                    return

        with CS(REF_POS):
            # Create tag
            CS.store(m_tag.set(*current_tag))
            tag_route = CS.get_route_parse()

        # Create bridge tag
        CS.store(m_bridge_tag.set(
            CS.ref(m_file.fid, REF_ROOT),
            CS.ref(m_tag.tag_id, *tag_route),
            self.extent.line_pos[0],
            self.extent.line_pos[1],
            self.extent.char_pos[0],
            self.extent.char_pos[1],
        ))
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Unknown AST default."""
        # Create ast
        with CS(REF_POS):
            CS.store(m_ast.set(None, f"AST{len(CS.cs)}", 0))
            ast_id_route = CS.get_route_parse()
        
        with CS(REF_NO_REF):
            # Create ast_debug
            self.ast_debug(CS, ast_id_route)

            # Create tag and bridge_tag
            self.tag(CS, ast_id_route)
        return

    def extract_1arg(self, CS: ChangeSetType, type_id: int, name: int|str, extent: Line|None=None) -> None:
        """m_ast only handler."""
        # Create ast
        with CS(REF_POS):
            CS.store(m_ast.view(((m_ast.ast_id,),), None, name, type_id))
            ast_id_route = CS.get_route_parse()

        with CS(REF_NO_REF):
            # Create ast_debug
            if G.OVERRIDE_FORCE_AST_DEBUG:
                self.ast_debug(CS, ast_id_route)

            # Create tag and bridge_tag
            self.tag(CS, ast_id_route, extent)
        return


    def within_range(self, token, ast_kind) -> bool:
        """Check if token is within Type/CPPro. Called from Zone"""
        if not self.need_processing:
            return False
        match self.end_mode:
            case End_Mode.No_Check:
                self.extent.grow(Line(token.extent))
                return True
            case End_Mode.Auto:
                if ast_kind != AST_KIND.punctuation:
                    self.extent.grow(Line(token.extent))
                    return True
                if token.spelling_str == ";":
                    self.need_processing = False
                    return False
            case End_Mode.Semicolon:
                if ast_kind != AST_KIND.punctuation:
                    self.extent.grow(Line(token.extent))
                    return True
                if token.spelling_str == ";":
                    self.need_processing = False
                    return False
            case End_Mode.Comma:
                if ast_kind != AST_KIND.punctuation:
                    self.extent.grow(Line(token.extent))
                    return True
                if token.spelling_str == ",":
                    self.need_processing = False
                    return False
            case End_Mode.Extent:
                if not self.extent.is_inside(token.extent):
                    self.need_processing = False
                    return False
        self.extent.grow(Line(token.extent))
        return True

    def em_auto_check(self, cursor) -> None:
        """No-op: Extents are managed by parent zones and token delimiters."""
        return

    def exec_filter(self, token, cursor, kind) -> None:
        match kind:
            case AST_KIND.comment:
                return self.exec_comment(token, cursor)
            case AST_KIND.punctuation:
                return self.exec_punctuation(token, cursor)
            case AST_KIND.keyword:
                return self.exec_keyword(token, cursor)
            case AST_KIND.identifier:
                return self.exec_identifier(token, cursor)
            case AST_KIND.literal:
                return self.exec_literal(token, cursor)

    def exec_comment(self, token, cursor) -> None:
        return

    def exec_punctuation(self, token, cursor) -> None:
        return

    def exec_keyword(self, token, cursor) -> None:
        return

    def exec_identifier(self, token, cursor) -> None:
        return

    def exec_literal(self, token, cursor) -> None:
        return

    def clean(self):
        return


    def __str__(self) -> str:
        """Passthrough to good_looking_printing."""
        return good_looking_printing(self, COLOR.red(f"\n{type(self).__name__}: "))


class Ast_Comment(Ast):
    """type_id 1."""

    def __init__(self, extent: Line) -> None:
        self.extent = extent

    # rip this shit NEED TO REDO FOR USE WITH LINE
    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.C_Comment, self.comment, self.extent)
        return

class Ast_Keyword(Ast_Comment):
    """type_id 2."""

    def __init__(self, extent: Line) -> None:
        self.extent = extent

    # rip this shit
    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, 2, self.comment, self.extent)
        return

class Ast_MACRO_INSTANTIATION(Ast):
    def __init__(self, line: Line, func_name: Line) -> None:
        self.extent = line
        self.is_function = False
        self.function_args = []
        if self.extent != func_name:
            self.func_name = func_name

    def exec_comment(self, token, cursor):
        return

    def exec_punctuation(self, token, cursor):
        if self.is_function:
            if token.spelling_str == ",":
                self.function_args.append(Line())
            else:
                self.function_args[-1].new_end(token.extent)
        elif token.spelling_str == "(":
            self.function_args.append(Line())
            self.is_function = True
        
        return

    def exec_keyword(self, token, cursor):
        if self.is_function:
            self.function_args[-1].new_end(token.extent)

        return

    def exec_identifier(self, token, cursor):
        if self.is_function:
            self.function_args[-1].new_end(token.extent)

        return

    def exec_literal(self, token, cursor):
        if self.is_function:
            self.function_args[-1].new_end(token.extent)

        return


class AST_KIND(IntEnum):
    NOT_SET = 0
    comment = auto()
    punctuation = auto()
    keyword = auto()
    identifier = auto()
    literal = auto()

# RANGE 100 is reserved for CPPro
class CPPro(Ast):
    def __init__(self, cppro_ex: Line):
        self.ccpro_start_extent = cppro_ex
        self.need_processing = True
        self.end_mode = End_Mode.No_Check

    def ccpro_start_flip(self, cppro_class, *args) -> None:
        self.__class__ = cppro_class
        self.__init__(*args)
        
    def within_range(self, token, ast_kind) -> bool:
        return True

    def exec_comment(self, token, cursor) -> None:
        return

    def exec_punctuation(self, token, cursor) -> None:
        return

    def exec_keyword(self, token, cursor) -> None:
        self.exec_identifier(token, cursor)
        return 

    def exec_identifier(self, token, cursor) -> None:
        tspelling = token.spelling_str
        match tspelling:
            case 'if':
                self.ccpro_start_flip(CPPro_if, Line(cursor.extent))
            case 'elif':
                self.ccpro_start_flip(CPPro_elif, Line(cursor.extent))
            case 'else':
                self.ccpro_start_flip(CPPro_else, Line(cursor.extent))
            case 'endif':
                self.ccpro_start_flip(CPPro_endif, Line(cursor.extent))
            case 'ifdef':
                self.ccpro_start_flip(CPPro_ifdef, Line(cursor.extent))
            case 'ifndef':
                self.ccpro_start_flip(CPPro_ifndef, Line(cursor.extent))
            case 'elifdef':
                self.ccpro_start_flip(CPPro_elifdef, Line(cursor.extent))
            case 'elifndef':
                self.ccpro_start_flip(CPPro_elifndef, Line(cursor.extent))
            case 'define':
                self.ccpro_start_flip(CPPro_define, Line(cursor.extent))
            case 'undef':
                self.ccpro_start_flip(CPPro_undef, Line(cursor.extent))
            case 'embed':
                logger.warn(f"#embed detected {cursor.extent}, NOT IMPLEMENTED!!")
                return
            case 'line':
                self.ccpro_start_flip(CPPro_line, Line(cursor.extent))
            case 'error':
                self.ccpro_start_flip(CPPro_error, Line(cursor.extent))
            case 'warning':
                self.ccpro_start_flip(CPPro_warning, Line(cursor.extent))
            case 'pragma':
                self.ccpro_start_flip(CPPro_pragma, Line(cursor.extent))

            case _:
                logger.warn(f"CPPro>>Spelling:{tspelling},Kind:{cursor.kind} => Not implemented")
        return


    def exec_literal(self, token, cursor) -> None:
        return




class CPPro_if(Ast):
    """type_id 102."""

    def __init__(self, extent: Line) -> None:
        self.extent = extent
        self.need_processing = True
        self.end_mode = End_Mode.Extent
        self.expression = ""
        self.highlight = Line()
        self.endif = Line()

    def exec_comment(self, token, cursor) -> None:
        return

    def exec_punctuation(self, token, cursor) -> None:
        self.expression += token.spelling_str
        self.highlight.new_end(token.extent)
        return

    def exec_keyword(self, token, cursor) -> None:
        self.expression += token.spelling_str
        self.highlight.new_end(token.extent)
        return

    def exec_identifier(self, token, cursor) -> None:
        self.expression += token.spelling_str
        self.highlight.new_end(token.extent)
        return

    def exec_literal(self, token, cursor) -> None:
        self.expression += token.spelling_str
        self.highlight.new_end(token.extent)
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_if, self.expression, self.extent)
        return


class CPPro_elif(CPPro_if):
    """type_id 103."""

    def exec_comment(self, token, cursor) -> None:
        return

    def exec_punctuation(self, token, cursor) -> None:
        self.expression += token.spelling_str
        self.highlight.new_end(token.extent)
        return

    def exec_keyword(self, token, cursor) -> None:
        self.expression += token.spelling_str
        self.highlight.new_end(token.extent)
        return

    def exec_identifier(self, token, cursor) -> None:
        self.expression += token.spelling_str
        self.highlight.new_end(token.extent)
        return

    def exec_literal(self, token, cursor) -> None:
        self.expression += token.spelling_str
        self.highlight.new_end(token.extent)
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_elif, self.expression, self.extent)
        return



class CPPro_else(Ast):
    """type_id 104."""

    def __init__(self, extent: Line) -> None:
        self.extent = extent
        self.need_processing = False
        self.end_mode = End_Mode.Extent
        self.expression = ""
        self.endif = Line()

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_else, "", self.extent)
        return


class CPPro_endif(Ast):
    """type_id 105."""

    def __init__(self, extent: Line) -> None:
        self.extent = extent
        self.need_processing = False
        self.end_mode = End_Mode.Extent

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_endif, "", self.extent)
        return

class CPPro_ifdef(CPPro_if):
    """type_id 100."""

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_ifdef, self.expression, self.extent)
        return


class CPPro_ifndef(CPPro_if):
    """type_id 101."""

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_ifndef, self.expression, self.extent)
        return

class CPPro_elifdef(CPPro_elif):
    """type_id 999."""

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_elifdef, self.expression, self.extent)
        return

class CPPro_elifndef(CPPro_elif):
    """type_id 999."""

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_elifndef, self.expression, self.extent)
        return


class CPPro_define(Ast):
    """type_id 106."""

    def __init__(self, extent: Line) -> None:
        self.extent = extent
        self.need_processing = True
        self.end_mode = End_Mode.Extent
        self.identifier = ""
        self.func_args = []
        self.func_enabled = False
        self.replacement = ""
        self.highlight = Line()
        self.highlight_replacement = Line()

    def exec_comment(self, token, cursor) -> None:
        return

    def exec_punctuation(self, token, cursor) -> None:
        spelling = token.spelling_str
        if self.func_enabled:
            if spelling == ")":
                self.func_enabled = False
                return
            elif spelling == ",":
                self.func_args.append("")
            else:
                self.func_args[-1] += spelling
            return
        
        elif self.replacement == "" and spelling == "(":
            self.func_enabled = True
            self.func_args.append("")
            return

        self.replacement += spelling
        self.highlight_replacement.new_end(token.extent)
        return

    def exec_keyword(self, token, cursor) -> None:
        spelling = token.spelling_str
        if self.identifier == "":
            self.identifier += spelling
            self.highlight.new_end(token.extent)
            return

        if self.func_enabled:
            self.func_args[-1] += spelling
            return

        self.replacement += spelling
        self.highlight_replacement.new_end(token.extent)
        return

    def exec_identifier(self, token, cursor) -> None:
        spelling = token.spelling_str
        if self.identifier == "":
            self.identifier += spelling
            self.highlight.new_end(token.extent)
            return

        if self.func_enabled:
            self.func_args[-1] += spelling
            return

        self.replacement += spelling
        self.highlight_replacement.new_end(token.extent)
        return

    def exec_literal(self, token, cursor) -> None:
        spelling = token.spelling_str
        if self.func_enabled:
            self.func_args[-1] += spelling
            return

        self.replacement += spelling
        self.highlight_replacement.new_end(token.extent)
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        if self.replacement is None:
            # Empty define
            self.extract_1arg(CS, ASTT.CPPro_define, self.identifier, self.extent)
            return

        # BAD IMPLEMENTATION, NEEDS TO BE FIXED, WE NEED RECURSIVE DETECTION FOR 2ND ARG
        self.extract_1arg(CS, ASTT.CPPro_define_macro, self.identifier, self.extent)

        return


class CPPro_undef(Ast):
    """type_id 107."""

    def __init__(self, extent: Line) -> None:
        self.extent = extent
        self.need_processing = True
        self.end_mode = End_Mode.Extent
        self.identifier = ""
        self.highlight = Line()

    def exec_comment(self, token, cursor) -> None:
        return

    def exec_punctuation(self, token, cursor) -> None:
        self.identifier += token.spelling_str
        self.highlight.new_end(token.extent)
        return

    def exec_keyword(self, token, cursor) -> None:
        self.identifier += token.spelling_str
        self.highlight.new_end(token.extent)
        return

    def exec_identifier(self, token, cursor) -> None:
        self.identifier += token.spelling_str
        self.highlight.new_end(token.extent)
        return

    def exec_literal(self, token, cursor) -> None:
        self.identifier += token.spelling_str
        self.highlight.new_end(token.extent)
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_undef, self.identifier, self.extent)
        return


class CPPro_include(Ast):
    """type_id 108."""

    def __init__(self, extent: Line) -> None:
        self.extent = extent
        self.need_processing = True
        self.end_mode = End_Mode.Extent
        self.a_include = None
        self.w_include = ""
        self.highlight = Line()
        self.debug = None

    def exec_comment(self, token, cursor) -> None:
        return

    def exec_punctuation(self, token, cursor) -> None:
        self.w_include += token.spelling_str
        if self.debug:
            self.a_include = self.w_include
        self.highlight.new_end(token.extent)
        return

    def exec_keyword(self, token, cursor) -> None:
        spelling = token.spelling_str
        if self.a_include is None and spelling == "include":
            try:
                inc_file = cursor.get_included_file()
                if G.MF and inc_file:
                    self.a_include = G.MF.resolve_path(inc_file.name)
                else:
                    self.debug = "#FAILED_RESOLVE"
            except (AssertionError, Exception):
                self.debug = "#FAILED_RESOLVE"
            return

        self.w_include += spelling
        if self.debug:
            self.a_include = self.w_include
        self.highlight.new_end(token.extent)
        
        return

    def exec_identifier(self, token, cursor) -> None:
        spelling = token.spelling_str
        if self.a_include is None and spelling == "include":
            try:
                inc_file = cursor.get_included_file()
                if G.MF and inc_file:
                    self.a_include = G.MF.resolve_path(inc_file.name)
                else:
                    self.debug = "#FAILED_RESOLVE"
            except (AssertionError, Exception):
                self.debug = "#FAILED_RESOLVE"
            return

        self.w_include += spelling
        if self.debug:
            self.a_include = self.w_include
        self.highlight.new_end(token.extent)
        
        return

    def exec_literal(self, token, cursor) -> None:
        self.w_include += token.spelling_str
        if self.debug:
            self.a_include = self.w_include
        self.highlight.new_end(token.extent)
        return


    #################################REMEMBER TO CLEAN A_INCLUDE IF DEBUG ISN'T NONE
    def extract(self, CS: ChangeSetType) -> None:
        """Extract with m_ast_include."""
        include_target = self.a_include if self.a_include is not None else self.w_include
        with CS(REF_POS):
            CS.store(m_file_name.get_set(None, include_target))
            fnid_route = CS.get_route_parse()
        
        with CS(REF_POS):
            # Create ast
            CS.store(m_ast.view(
                ((m_ast.ast_id, m_ast_include.ast_id, 1),),
                None,
                self.w_include,
                ASTT.CPPro_include,
                None,
                CS.ref(m_file_name.fnid, *fnid_route),
            ))
            ast_id_route = CS.get_route_parse()

        with CS(REF_NO_REF):
            # Create ast_debug
            if G.OVERRIDE_FORCE_AST_DEBUG:
                self.ast_debug(CS, ast_id_route)

            # Create tag and bridge_tag
            self.tag(CS, ast_id_route, self.extent)
        return

##NOT IN USE
class CPPro_embed(Ast):
    """type_id 999."""

    def __init__(self, extent: Line) -> None:
        self.extent = extent
        self.need_processing = True
        self.end_mode = End_Mode.Extent
        #self.identifier = identifier

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, 999, self.identifier, self.extent)
        return


class CPPro_line(Ast):
    """type_id 109."""

    def __init__(self, extent: Line, lineno: int | str = "", filename: str | None = None) -> None:
        self.extent = extent
        self.need_processing = True
        self.end_mode = End_Mode.Extent
        self.lineno = lineno
        self.hl_lineno = Line()
        self.filename = filename
        self.hl_filename = Line()

    def exec_comment(self, token, cursor) -> None:
        return

    def exec_punctuation(self, token, cursor) -> None:
        spelling = token.spelling_str
        if self.lineno == "":
            self.lineno = spelling
            self.hl_lineno = Line(token.extent)
            return

        self.filename = spelling
        self.hl_filename = Line(token.extent)
        return

    def exec_keyword(self, token, cursor) -> None:
        spelling = token.spelling_str
        if self.lineno == "":
            self.lineno = spelling
            self.hl_lineno = Line(token.extent)
            return

        self.filename = spelling
        self.hl_filename = Line(token.extent)
        return

    def exec_identifier(self, token, cursor) -> None:
        spelling = token.spelling_str
        if self.lineno == "":
            self.lineno = spelling
            self.hl_lineno = Line(token.extent)
            return

        self.filename = spelling
        self.hl_filename = Line(token.extent)
        return

    def exec_literal(self, token, cursor) -> None:
        spelling = token.spelling_str
        if self.lineno == "":
            self.lineno = spelling
            self.hl_lineno = Line(token.extent)
            return

        self.filename = spelling
        self.hl_filename = Line(token.extent)
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        if self.filename is None:
            self.extract_1arg(CS, ASTT.CPPro_line, f"{self.lineno}", self.extent)
        else:
            self.extract_1arg(CS, ASTT.CPPro_line, f"{self.lineno} {self.filename}", self.extent)
        return


class CPPro_error(Ast):
    """type_id 110."""

    def __init__(self, extent: Line, msg: str = "") -> None:
        self.extent = extent
        self.need_processing = True
        self.end_mode = End_Mode.Extent
        self.msg = msg
        self.hl_msg = Line()

    def exec_comment(self, token, cursor) -> None:
        return

    def exec_punctuation(self, token, cursor) -> None:
        self.msg += token.spelling_str
        self.hl_msg.new_end(token.extent)
        return

    def exec_keyword(self, token, cursor) -> None:
        self.msg += token.spelling_str
        self.hl_msg.new_end(token.extent)
        return

    def exec_identifier(self, token, cursor) -> None:
        self.msg += token.spelling_str
        self.hl_msg.new_end(token.extent)
        return

    def exec_literal(self, token, cursor) -> None:
        self.msg += token.spelling_str
        self.hl_msg.new_end(token.extent)
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_error, self.msg, self.extent)
        return

class CPPro_warning(CPPro_error):
    """type_id 999."""

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_warning, self.msg, self.extent)
        return


class CPPro_pragma(CPPro_error):
    """type_id 111."""

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, ASTT.CPPro_pragma, self.msg, self.extent)
        return

##NOT IN USE
class CPPro_defined(Ast):
    """type_id 999."""

    def __init__(self, extent: Line) -> None:
        self.extent = extent
        self.need_processing = True
        self.end_mode = End_Mode.Extent
        #self.defined = defined

    def extract(self, CS: ChangeSetType) -> None:
        """Passthrough to AST.extract_1arg."""
        self.extract_1arg(CS, 999, self.defined, self.extent)
        return

class End_Mode(IntEnum):
    No_Check = 0
    Auto = 1
    Semicolon = 2
    Comma = 3
    Extent = 4


class CQual(Flag):
    Empty = 0
    const = 1
    volatile = 2
    restrict = 4
    _Atomic = 8

    def output_ast(self) -> tuple | None:
        if self.value == 0:
            return None
        
        current_list = []

        self.value

        if CQual.const in self:
            current_list.append(ASTT.C_Qconst)
        if CQual.volatile in self:
            current_list.append(ASTT.C_Qvolatile)
        if CQual.restrict in self:
            current_list.append(ASTT.C_Qrestrict)
        if CQual._Atomic in self:
            current_list.append(ASTT.C_Q_Atomic)

        return tuple(current_list)

def get_decl_type(ast_type: int) -> int:
    """Return declaration AST type (e.g. C_struct -> C_structdecl)."""
    match ast_type:
        case ASTT.C_struct:
            return ASTT.C_structdecl
        case ASTT.C_union:
            return ASTT.C_uniondecl
        case ASTT.C_enum:
            return ASTT.C_enumdecl
        case ASTT.C_functionproto:
            return ASTT.C_functionprotodecl
        case _:
            return ast_type + 1


def get_notbind_type(ast_type: int) -> int:
    """Return unbound forward AST type (e.g. C_struct -> C_structnotbind)."""
    match ast_type:
        case ASTT.C_struct:
            return ASTT.C_structnotbind
        case ASTT.C_union:
            return ASTT.C_unionnotbind
        case ASTT.C_enum:
            return ASTT.C_enumnotbind
        case ASTT.C_functionproto:
            return ASTT.C_functionprotnotbind
        case _:
            return ast_type + 2


class Not_Implemented(Ast):
    def __init__(self, extent: Line, end_mode: int=End_Mode.Extent) -> None:
        self.extent = extent
        self.end_mode = end_mode
        # Flag to be set to False once we are past the appropriate extent of the type.
        self.need_processing = True
        self.data = []
        self.brace_depth = 0

    def within_range(self, token, ast_kind) -> bool:
        """Swallow all tokens within compound statement / unhandled block."""
        if not self.need_processing:
            return False

        if ast_kind == AST_KIND.punctuation:
            tspelling = token.spelling_str
            if tspelling == "{":
                self.brace_depth += 1
            elif tspelling == "}":
                self.brace_depth -= 1
                if self.brace_depth <= 0:
                    self.extent.grow(Line(token.extent))
                    self.need_processing = False
                    return True

        if self.end_mode == End_Mode.Extent and not self.extent.is_inside(token.extent):
            if self.brace_depth <= 0:
                self.need_processing = False
                return False

        self.extent.grow(Line(token.extent))
        return True

    def exec_filter(self, token, cursor, ast_kind):
        if ast_kind == AST_KIND.comment:
            return
        self.data.append(token.spelling_str)
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Do not emit AST database records for unextracted code blocks."""
        return


class AST_Expression(Ast):
    pass


class AST_Enum_Equal(AST_Expression):
    def __init__(self, extent: Line, end_mode: int=End_Mode.Extent) -> None:
        self.extent = extent
        self.end_mode = end_mode
        # Flag to be set to False once we are past the appropriate extent of the type.
        self.need_processing = True
        self.data = []

    def exec_filter(self, token, cursor, ast_kind):
        if ast_kind == AST_KIND.comment:
            return
        if ast_kind == AST_KIND.punctuation:
            if token.spelling_str == "}":
                self.extent.new_end_reversed(Line(token.extent))
                self.need_processing = False
                return
        self.data.append(token.spelling_str)
        return

class AST_Array(AST_Expression):
    def __init__(self, extent: Line, end_mode: int=End_Mode.Extent) -> None:
        self.extent = extent
        self.end_mode = end_mode
        # Flag to be set to False once we are past the appropriate extent of the type.
        self.need_processing = True
        self.data = []
        self.bracket_depth = 1

    def within_range(self, token, ast_kind) -> bool:
        if not self.need_processing:
            return False

        if ast_kind == AST_KIND.punctuation:
            spelling = token.spelling_str
            if spelling == "[":
                self.bracket_depth += 1
            elif spelling == "]":
                self.bracket_depth -= 1
                if self.bracket_depth <= 0:
                    self.extent.new_end_reversed(Line(token.extent))
                    self.need_processing = False
                    return True

        self.extent.grow(Line(token.extent))
        return True

    def exec_filter(self, token, cursor, ast_kind):
        if ast_kind == AST_KIND.comment:
            return
        self.data.append(token.spelling_str)
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Do not emit AST database records for array size expressions."""
        return


class AST_Initializer(AST_Expression):
    def __init__(self, extent: Line, end_mode: int=End_Mode.Extent) -> None:
        self.extent = extent
        self.end_mode = end_mode
        self.need_processing = True
        self.data = []
        self.brace_depth = 0
        self.paren_depth = 0
        self.bracket_depth = 0

    def within_range(self, token, ast_kind) -> bool:
        if not self.need_processing:
            return False

        if ast_kind == AST_KIND.punctuation:
            spelling = token.spelling_str
            if spelling == "{":
                self.brace_depth += 1
            elif spelling == "}":
                self.brace_depth = max(0, self.brace_depth - 1)
            elif spelling == "(":
                self.paren_depth += 1
            elif spelling == ")":
                self.paren_depth = max(0, self.paren_depth - 1)
            elif spelling == "[":
                self.bracket_depth += 1
            elif spelling == "]":
                self.bracket_depth = max(0, self.bracket_depth - 1)
            elif spelling in (";", ","):
                if self.brace_depth == 0 and self.paren_depth == 0 and self.bracket_depth == 0:
                    self.extent.new_end_reversed(Line(token.extent))
                    self.need_processing = False
                    return False

        self.extent.grow(Line(token.extent))
        return True

    def exec_filter(self, token, cursor, ast_kind):
        if ast_kind == AST_KIND.comment:
            return
        self.data.append(token.spelling_str)
        return

    def extract(self, CS: ChangeSetType) -> None:
        """Do not emit AST database records for variable initializer expressions."""
        return


class TypeToken():
    def __init__(self, token, asttype=0) -> None:
        self.extent = getattr(token, 'line', None) or Line(token.extent)
        self.code = getattr(token, 'spelling_str', '')
        self.type = asttype
        self.is_definition = False
        self.foreign_name = None
        self.foreign_file = None
        self.foreign_extent = None
        return

    def __repr__(self):
        return self.code

    def get_foreign(self, cursor) -> None:
        if cursor.is_definition():
            self.is_definition = True
            return

        if (foreign_cursor := cursor.get_definition()) is None:
            return
        self.foreign_name = safe_cursor_spelling(foreign_cursor)
        self.foreign_file = foreign_cursor.extent.start.file
        self.foreign_extent = Line(foreign_cursor.extent)
        return


class TSRef(IntEnum):
    No_Ref = 0
    AST_Ref = 1
    Route_Ref = 2


class TypeSegment():
    def __init__(self) -> None:
        self.content = []
        self.cqual = CQual.Empty
        self.cqual_content = []
        self.ref_type = TSRef.No_Ref
        self.ref = None

        self.type_id = None
        self.ref_ast_id = None
        return

    def __repr__(self):
        return f"TypeSegment:{self.cqual_content} {self.content}"

    def __bool__(self) -> bool:
        return bool(self.content or self.cqual_content)

    def is_definition(self):
        return any(typetoken.is_definition for typetoken in self.content)

    def extend(self, typesegment) -> None:
        self.content.extend(typesegment.content)
        self.cqual = self.cqual | typesegment.cqual
        self.cqual_content.extend(typesegment.cqual_content)
        return

    def append(self, item) -> None:
        self.content.append(item)
        return

    def append_cqal(self, item) -> None:
        self.cqual_content.append(item)
        return

    def get_foreign(self, cursor) -> None:
        self.content[-1].get_foreign(cursor)
        return

    def generate_ast(self, CS: ChangeSetType) -> None:
        self.type_id = None
        self.ref_ast_id = None

        if self.ref_type == TSRef.No_Ref:
            if len(self.content) == 1 and self.cqual == CQual.Empty:
                self.type_id = self.content[0].type
                return

            if (
                self.content
                and self.content[0].type in (ASTT.C_struct, ASTT.C_functionproto, ASTT.C_union, ASTT.C_enum)
                and len(self.content) == 2
                and self.cqual == CQual.Empty
            ):
                notbind_type = get_notbind_type(self.content[0].type)
                with CS(REF_POS):
                    CS.store(m_ast.get_set(
                        None,
                        self.content[1].code,
                        notbind_type,
                    ))
                    route_key = CS.get_route_parse()
                    self.type_id = self.content[0].type
                    self.ref_type = TSRef.Route_Ref
                    self.ref = route_key
                return

            compound = []
            if (cqual_out := self.cqual.output_ast()) is not None:
                for item in cqual_out:
                    compound.append((item, 0))

            for i, typetoken in enumerate(self.content):
                if typetoken.type in (ASTT.C_struct, ASTT.C_functionproto, ASTT.C_union, ASTT.C_enum):
                    if i > 0 and self.content[i - 1].type == typetoken.type:
                        notbind_type = get_notbind_type(typetoken.type)
                        with CS(REF_POS):
                            CS.store(m_ast.get_set(
                                None,
                                typetoken.code,
                                notbind_type,
                            ))
                            compound.append((typetoken.type, CS.ref(m_ast.ast_id)))
                else:
                    compound.append((typetoken.type, 0))

            view = []
            for i, item in enumerate(compound):
                t_code = int(item[0]) if hasattr(item[0], "value") else int(item[0])
                view.extend((None, i, t_code, item[1]))

            view = tuple(view)
            with CS(REF_POS):
                CS.store(m_ast.view(
                    ((m_ast.ast_id, m_ast_container.ast_id, len(compound)),),
                    None,
                    "",
                    ASTT.C_Compound,
                    *view,
                ))
                route_key = CS.get_route_parse()

                self.ref_type = TSRef.Route_Ref
                self.ref = route_key


class Zone_Type(IntEnum):
    Unset = 0
    Function_Args = 1
    Declared_Args = 2
    Compound_Stmt = 3
    Array_Content = 4
    Enum_Content = 5
    Enum_Equal = 6
    Full_File = 7
    Initializer_Expr = 8


class Zone:
    """Represent spatial code scoping boundaries for parsing C code blocks in isolation.
    
    `Zone` instances scope sections of C code (such as function parameters, compound statements,
    or enum bodies). During ChangeSet extraction (`.extract(CS)`), `Zone` instances act as position
    locators, providing the stored operation index in `CS.cs` so parent and child AST nodes can resolve
    relational foreign keys via `CS.ref(...)`.
    """
    def __init__(self, zone_type:int, cursors_array) -> None:
        self.zone_type = zone_type
        self.extent = Line()
        self.preset_extents = []
        self.children = []
        self.ast_type = C_Type
        self.end_mode = End_Mode.Auto
        self.completed = False

        if cursors_array and zone_type != Zone_Type.Full_File:
            def _get_cur_file_name(c):
                f = getattr(c, "_file_name", None)
                if f is None:
                    file_obj = c.extent.start.file
                    f = file_obj.name if file_obj else ""
                    c._file_name = f
                return f

            cur_file = None
            for c in cursors_array:
                fn = _get_cur_file_name(c)
                if fn:
                    cur_file = fn
                    break
            if cur_file:
                cursors_array = tuple(
                    c for c in cursors_array if not _get_cur_file_name(c) or _get_cur_file_name(c) == cur_file
                )

        if zone_type == Zone_Type.Compound_Stmt:
            for cursor in cursors_array:
                self.extent.grow(Line(cursor.extent))
            self.children.append(Not_Implemented(self.extent))

        elif zone_type == Zone_Type.Enum_Equal:
            for cursor in cursors_array:
                self.extent.grow(Line(cursor.extent))
            self.children.append(AST_Enum_Equal(self.extent))

        elif zone_type == Zone_Type.Initializer_Expr:
            for cursor in cursors_array:
                self.extent.grow(Line(cursor.extent))
            self.children.append(AST_Initializer(self.extent))

        elif zone_type == Zone_Type.Array_Content:
            for cursor in cursors_array:
                self.extent.grow(Line(cursor.extent))
            self.children.append(AST_Array(self.extent))

        elif zone_type != Zone_Type.Full_File:
            for cursor in cursors_array:
                temp_ext = Line(cursor.extent)
                self.extent.grow(temp_ext)
                self.preset_extents.append(temp_ext)

        if zone_type == Zone_Type.Enum_Content:
            self.end_mode = End_Mode.Comma

    def check_exec(self, token, cursor, ast_kind):
        """Check if extent is part of Zone and exec if needed. Return True on exec."""
        if self.completed:
            return False

        # Fast exit for comments - comments do not need AST token extraction
        if ast_kind == AST_KIND.comment:
            return True

        tline = getattr(token, 'line', None) or Line(token.extent)
        tspelling = getattr(token, 'spelling_str', '')

        if ast_kind == AST_KIND.punctuation and tspelling == "}":
            if self.zone_type in (Zone_Type.Declared_Args, Zone_Type.Enum_Content, Zone_Type.Compound_Stmt):
                self.extent.grow(tline)
                self.preset_extents.clear()
                self.completed = True
                return True

        if (not self.extent.is_inside(tline)) and (self.zone_type != Zone_Type.Full_File):
            if not (self.children and self.children[-1].need_processing):
                return False

        # Fast path, the last children added
        if self.children:
            if self.children[-1].within_range(token, ast_kind):
                self.children[-1].exec_filter(token, cursor, ast_kind)
                return True
            if ast_kind == AST_KIND.punctuation:
                if tspelling == "*":
                    self.children[-1].extent.grow(cursor.extent)
                    self.children[-1].need_processing = True
                    self.children[-1].exec_filter(token, cursor, ast_kind)
                    return True

        if ast_kind == AST_KIND.punctuation:
            # Commonly found between extents, Processing not needed.
            if tspelling in (";", ",", ")", "}"):
                return True
            # While were in punctuation, lets handle CPPros
            if tspelling == "#":
                kind = cursor.kind
                if kind == cc.CursorKind.PREPROCESSING_DIRECTIVE:
                    self.children.append(CPPro(tline))
                    return True
                elif kind == cc.CursorKind.INCLUSION_DIRECTIVE: 
                    self.children.append(CPPro_include(Line(cursor.extent)))
                    return True

        # Guard against statement keywords outside type declarations
        if ast_kind == AST_KIND.keyword and tspelling in (
            "if", "else", "return", "switch", "case", "default", "break", "continue", "for", "while", "do", "goto"
        ):
            return True

        # Check for valid preset_extents
        if self.preset_extents:
            while self.preset_extents and self.preset_extents[0].line_pos[1] < tline.line_pos[0]:
                self.preset_extents.pop(0)

            for i, p_extent in enumerate(self.preset_extents):
                if p_extent.is_inside(tline):
                    # Quick reminder that libclang won't give us the extent of CPPros here...
                    self.children.append(self.ast_type(p_extent, End_Mode.Extent))
                    self.children[-1].exec_filter(token, cursor, ast_kind)

                    del self.preset_extents[i]
                    return True

        self.children.append(self.ast_type(tline, self.end_mode))
        self.children[-1].exec_filter(token, cursor, ast_kind)

        return True

    def extract(self, CS: ChangeSetType) -> None:
        for item in self.children:
            if isinstance(item, C_Type):
                item.extract(CS)
            else:
                with CS(REF_NO_REF):
                    item.extract(CS)

        return


    def gen_lined_dict(self):
        self.A_Line_Dict = {}
        for item in self.children:
            if self.A_Line_Dict.get(item.extent.line_pos[0]) is None:
                self.A_Line_Dict[item.extent.line_pos[0]] = []
            self.A_Line_Dict[item.extent.line_pos[0]].append(item)

        return


class C_Type(Ast):
    """Represent C type declarations, specifiers, qualifiers (`const`, `volatile`), and declarators.
    
    `C_Type` parses individual C statement lines or type declarations. It uses three internal mechanisms
    to accurately handle type qualifiers and pointers:
    - `self.content`: Accumulates current tokens until a swap trigger is encountered.
    - `self.swap_out()`: Moves accumulated tokens from `content` into structured `typedata`.
    - `self.typedata`: Stores list of `TypeSegment` tuples `([ASTT, ...], CQUAL)` maintaining type ordering.
    """
    def __init__(self, extent: Line, end_mode: int=End_Mode.Auto) -> None:
        self.extent = extent
        self.end_mode = end_mode
        self.name = ""
        # Flag to be set to False once we are past the appropriate extent of the type.
        self.need_processing = True
        # All Zones within this type.
        self.zones = []
        # Content being process, this is temporary and should end up in root.
        self.content = TypeSegment()
        # Final form of current type (pointer/const might need to be right merged).
        # Contains multiple ​​TypeSegment: ([ASTT, ASTT....],CQUAL)
        self.typedata = []
        # Allow us to capture arg from non func_decl function proto and
        # allow us to keep pointer+func_proto inside the same root index.
        self.func_proto = False

        self.storage_class = None

    def within_range(self, token, ast_kind) -> bool:
        """Check if token is within Type/Declaration scope. Called from Zone."""
        if not self.need_processing:
            return False

        tline = getattr(token, 'line', None) or Line(token.extent)
        tspelling = getattr(token, 'spelling_str', '')

        if self.zones:
            for zone in self.zones:
                if getattr(zone, "completed", False):
                    continue
                if zone.extent.is_inside(tline) or (zone.children and zone.children[-1].need_processing) or zone.preset_extents:
                    self.extent.grow(tline)
                    return True

        match self.end_mode:
            case End_Mode.No_Check:
                self.extent.grow(tline)
                return True
            case End_Mode.Auto | End_Mode.Semicolon:
                if ast_kind != AST_KIND.punctuation:
                    self.extent.grow(tline)
                    return True
                if tspelling == ";":
                    self.need_processing = False
                    return False
            case End_Mode.Comma:
                if ast_kind != AST_KIND.punctuation:
                    self.extent.grow(tline)
                    return True
                if tspelling == ",":
                    self.need_processing = False
                    return False
            case End_Mode.Extent:
                if not self.extent.is_inside(tline):
                    self.need_processing = False
                    return False

        self.extent.grow(tline)
        return True

    def extract(self, CS: ChangeSetType) -> None:
        # Makes sure that everything is in self.typedata
        if self.content:
            self.swap_out()

        # 1. Process Child Zones within Route
        zone_link = ()
        if self.zones:
            with CS(REF_MULTI):
                for zone in self.zones:
                    zone.extract(CS)
                zone_link = CS.route[-2:]

        # 2. Parse self.typedata into distinct declarations (final_types)
        # Groups shared base type specifier with individual declarators
        root_type = TypeSegment()
        type_constructor = []
        final_types = []

        for typesegment in self.typedata:
            if not root_type:
                root_type = typesegment

            type_constructor.append(typesegment)

            # Detects variable identifier tokens (type == 0) to split multiple declarators
            if typesegment.content and typesegment.content[-1].type == 0:
                final_types.append(tuple(type_constructor))
                type_constructor = [root_type]

        if not final_types and type_constructor:
            final_types.append(tuple(type_constructor))

        # 3. Zone Handling for Type Definitions (struct/union/enum/functionproto declarations)
        if self.zones:
            for final_type in final_types:
                for typesegment in final_type:
                    if typesegment.is_definition():
                        for item in typesegment.content:
                            if item.is_definition and item.type in (ASTT.C_struct, ASTT.C_functionproto, ASTT.C_union, ASTT.C_enum):
                                decl_type = get_decl_type(item.type)
                                # Push the defined struct/union/enum to CS via ref_view
                                with CS(REF_POS):
                                    CS.store(m_ast.ref_view(
                                        ((m_ast.ast_id,),),
                                        None,
                                        item.code,
                                        decl_type,
                                        (
                                            (  # If condition: any child m_ast in zone
                                                (m_ast.ast_id, None),
                                            ),
                                            (  # Then / Else branches
                                                (
                                                    ((m_ast.ast_id, m_ast_container.ast_id, 1),),
                                                    (None, ("rank",), m_ast.type_id, m_ast.ast_id),
                                                ),
                                            ),
                                            tuple(zone_link),
                                        ),
                                    ))
                                    ast_id_route = CS.get_route_parse()
                                    typesegment.ref_type = TSRef.Route_Ref
                                    typesegment.ref = ast_id_route

                                # For standalone type definitions (no variable declarator in final_type),
                                # tag and debug the canonical declaration AST directly here
                                has_var = any(any(token.type == 0 for token in ts.content) for ts in final_type)
                                if not has_var:
                                    with CS(REF_NO_REF):
                                        if G.OVERRIDE_FORCE_AST_DEBUG:
                                            self.ast_debug(CS, ast_id_route)
                                        self.tag(CS, ast_id_route, self.extent)

        # 4. Insert ASTs into ChangeSet
        for final_type in final_types:
            if not final_type:
                continue

            # Skip standalone type definitions that were already emitted and tagged in Step 3
            has_var = any(any(token.type == 0 for token in ts.content) for ts in final_type)
            is_type_def = any(ts.is_definition() for ts in final_type)
            if self.zones and is_type_def and not has_var:
                continue

            cs_inserter = []
            name = ""

            # Fetch / Generate Type Segment ASTs
            for typesegment in final_type:
                typesegment.generate_ast(CS)

            for i, typesegment in enumerate(final_type):
                # Search for identifier token if available
                for item in typesegment.content:
                    if item.type == 0:
                        name = item.code

                t_id = typesegment.type_id
                if t_id is None:
                    if typesegment.ref_type == TSRef.Route_Ref and not typesegment.content:
                        t_id = ASTT.C_Compound
                    elif typesegment.content:
                        t_id = typesegment.content[0].type
                    else:
                        t_id = 0

                if typesegment.ref_type == TSRef.Route_Ref:
                    ref_ast_id = CS.ref(m_ast.ast_id, *typesegment.ref)
                elif typesegment.ref_type == TSRef.AST_Ref:
                    ref_ast_id = typesegment.ref
                else:
                    ref_ast_id = typesegment.ref_ast_id if typesegment.ref_ast_id is not None else 0

                cs_inserter.extend((None, i, t_id, ref_ast_id))

            if not name:
                name = self.name

            if len(final_type) == 1:
                main_t_id = final_type[0].type_id
                if main_t_id is None:
                    if final_type[0].content:
                        main_t_id = final_type[0].content[0].type
                    else:
                        main_t_id = ASTT.C_Compound
            else:
                main_t_id = ASTT.C_Compound

            with CS(REF_POS):
                CS.store(m_ast.view(
                    ((m_ast.ast_id, m_ast_container.ast_id, len(final_type)),),
                    None,
                    name,
                    main_t_id,
                    *cs_inserter,
                ))
                ast_id_route = CS.get_route_parse()

            with CS(REF_NO_REF):
                if G.OVERRIDE_FORCE_AST_DEBUG:
                    self.ast_debug(CS, ast_id_route)

                self.tag(CS, ast_id_route, self.extent)

        CS.debug.append(final_types)
        return

    def swap_out(self):
        """Swap out content to root, allows correct pointer/const values."""
        self.typedata.append(self.content)
        self.content = TypeSegment()
        return

    def keyword_parse(self, token, cursor) -> None:
        """Allow to parse all possible keywords within C Types."""
        tspelling = getattr(token, 'spelling_str', '')
        match tspelling:
            # Storage Class
            case "auto":
                self.storage_class = TypeToken(token, ASTT.C_SCauto)
            case "register":
                self.storage_class = TypeToken(token, ASTT.C_SCregister)
            case "static":
                self.storage_class = TypeToken(token, ASTT.C_SCstatic)
            case "extern":
                self.storage_class = TypeToken(token, ASTT.C_SCextern)
            case "_Thread_local" | "thread_local" | "__thread":
                self.storage_class = TypeToken(token, ASTT.C_SC_Thread_local)
            case "typedef":
                self.storage_class = TypeToken(token, ASTT.C_SCtypedef)
            case "constexpr":
                self.storage_class = TypeToken(token, ASTT.C_SCconstexpr)
                logger.warn("Ast_TYPE>>keyword_parse>>constexpr<<Not Implemented")

            # Qualifiers
            case "const" | "__const" | "__const__":
                if CQual.const in self.content.cqual:
                    self.swap_out()
                self.content.cqual = self.content.cqual | CQual.const
                self.content.append_cqal(TypeToken(token, ASTT.C_Qconst))
            case "volatile" | "__volatile" | "__volatile__":
                if CQual.volatile in self.content.cqual:
                    self.swap_out()
                self.content.cqual = self.content.cqual | CQual.volatile
                self.content.append_cqal(TypeToken(token, ASTT.C_Qvolatile))
            case "restrict" | "__restrict" | "__restrict__":
                if CQual.restrict in self.content.cqual:
                    self.swap_out()
                self.content.cqual = self.content.cqual | CQual.restrict
                self.content.append_cqal(TypeToken(token, ASTT.C_Qrestrict))
            case "_Atomic":
                if CQual._Atomic in self.content.cqual:
                    self.swap_out()
                self.content.cqual = self.content.cqual | CQual._Atomic
                self.content.append_cqal(TypeToken(token, ASTT.C__Atomic))

            # Function Specifiers
            case "inline" | "__inline" | "__inline__" | "__always_inline" | "__gnu_inline":
                self.content.append(TypeToken(token, ASTT.C_FSinline))
            case "_Noreturn":
                self.content.append(TypeToken(token, ASTT.C_FS_Noreturn))

            # Alignment & Size Specifiers / Operators
            case "_Alignas" | "alignas":
                self.content.append(TypeToken(token, ASTT.C_AS__Alignas))
            case "sizeof" | "_Alignof" | "alignof" | "__alignof__" | "__alignof":
                logger.debug(f"C_Type>>size/align operator {tspelling}")
                return
            case "typeof" | "__typeof__" | "__typeof":
                logger.debug(f"C_Type>>typeof operator {tspelling}")
                return
            case "__attribute__" | "__attribute" | "__declspec":
                logger.debug(f"C_Type>>attribute {tspelling}")
                return
            case "__extension__":
                return
            case "asm" | "__asm__" | "__asm":
                logger.debug(f"C_Type>>asm keyword {tspelling}")
                return
            case "_Generic":
                return

            # Type specifiers
            case "struct":
                self.content.append(TypeToken(token, ASTT.C_struct))
            case "union":
                self.content.append(TypeToken(token, ASTT.C_union))
            case "enum":
                self.content.append(TypeToken(token, ASTT.C_enum))
            case "void":
                self.content.append(TypeToken(token, ASTT.C_void))

            case "unsigned" | "__unsigned" | "__unsigned__":
                self.content.append(TypeToken(token, ASTT.C_unsigned))
            case "signed" | "__signed" | "__signed__":
                self.content.append(TypeToken(token, ASTT.C_signed))

            case "char":
                self.content.append(TypeToken(token, ASTT.C_char))
                self.swap_out()
            case "short":
                self.content.append(TypeToken(token, ASTT.C_short))
                self.swap_out()
            case "int":
                self.content.append(TypeToken(token, ASTT.C_int))
                self.swap_out()
            case "long":
                self.content.append(TypeToken(token, ASTT.C_long))
                self.swap_out()
            case "Bool" | "_Bool" | "bool":
                self.content.append(TypeToken(token, ASTT.C_bool))
                self.swap_out()
            case "float":
                self.content.append(TypeToken(token, ASTT.C_float))
                self.swap_out()
            case "double":
                self.content.append(TypeToken(token, ASTT.C_double))
                self.swap_out()
            case "_Complex" | "_Imaginary" | "__int128" | "__int128_t" | "__uint128_t" | "__builtin_va_list":
                self.swap_out()

            # Predefined Identifiers in keyword context
            case "__func__" | "__FUNCTION__" | "__PRETTY_FUNCTION__":
                logger.debug(f"C_Type>>predefined identifier {tspelling} skipped")
                return

            # Statement keywords - safely skipped if encountered inside unhandled blocks
            case "if" | "else" | "return" | "switch" | "case" | "default" | "break" | "continue" | "for" | "while" | "do" | "goto":
                logger.debug(f"C_Type>>statement keyword {tspelling} skipped")
                return

            case _:
                logger.warn(f"C_Type>>keyword_parse>>{token.kind}|{tspelling}=> Not implemented")
        return

    def exec_comment(self, token, cursor):
        for zone in self.zones:
            if zone.check_exec(token, cursor, AST_KIND.comment):
                return

        return

    def exec_punctuation(self, token, cursor):
        tspelling = getattr(token, 'spelling_str', '')
        for zone in self.zones:
            if zone.check_exec(token, cursor, AST_KIND.punctuation):
                if tspelling == "}" and zone.zone_type == Zone_Type.Compound_Stmt:
                    if not any(ch.need_processing for ch in zone.children):
                        self.need_processing = False
                return

        match tspelling:
            case "*":
                self.content.append(TypeToken(token, ASTT.C_pointer))
                if self.func_proto:
                    self.content.append(TypeToken(token, ASTT.C_functionproto))
                    return
                self.swap_out()
            case "(":
                if self.func_proto:
                    self.func_proto = False
                elif (cursor.type.get_pointee().kind == cc.TypeKind.FUNCTIONPROTO) and (cursor.kind != cc.CursorKind.FUNCTION_DECL):
                    self.func_proto = True
                    return

                arg_children = []
                for kids in cursor.get_children():
                    if kids.kind == cc.CursorKind.PARM_DECL:
                        arg_children.append(kids)
                if arg_children:
                    self.zones.append(Zone(Zone_Type.Function_Args, arg_children))
            case "{":
                kind = cursor.kind
                if kind == cc.CursorKind.COMPOUND_STMT:
                    self.zones.append(Zone(Zone_Type.Compound_Stmt, (cursor,)))
                    return

                compound_kids = [k for k in cursor.get_children() if k.kind == cc.CursorKind.COMPOUND_STMT]
                if compound_kids:
                    self.zones.append(Zone(Zone_Type.Compound_Stmt, compound_kids))
                    return

                if kind == cc.CursorKind.FUNCTION_DECL:
                    self.zones.append(Zone(Zone_Type.Compound_Stmt, (cursor,)))
                    return

                zone_type = Zone_Type.Declared_Args
                if kind == cc.CursorKind.ENUM_DECL:
                    zone_type = Zone_Type.Enum_Content

                cur_file = cursor.extent.start.file.name if cursor.extent.start.file else None
                children = []
                for kids in cursor.get_children():
                    kid_file = kids.extent.start.file.name if kids.extent.start.file else None
                    if cur_file and kid_file and cur_file != kid_file:
                        continue
                    children.append(kids)

                if children:
                    self.zones.append(Zone(zone_type, children))
                elif zone_type == Zone_Type.Enum_Content:
                    self.zones.append(Zone(zone_type, (cursor,)))
            case "}":
                # When compound statement finishes, close function C_Type processing
                for zone in self.zones:
                    if zone.zone_type == Zone_Type.Compound_Stmt:
                        if not any(ch.need_processing for ch in zone.children):
                            self.need_processing = False
                            return
                    elif zone.zone_type in (Zone_Type.Declared_Args, Zone_Type.Enum_Content):
                        zone.completed = True
                        zone.preset_extents.clear()
            case "[":
                array_children = []
                for kids in cursor.get_children():
                    array_children.append(kids)

                if array_children:
                    self.content.append(TypeToken(token, ASTT.C_array))
                    self.zones.append(Zone(Zone_Type.Array_Content, array_children))
                else:
                    self.content.append(TypeToken(token, ASTT.C_arrayempty))
                self.swap_out()
            case "=":
                if cursor.kind in (cc.CursorKind.ENUM_DECL, cc.CursorKind.ENUM_CONSTANT_DECL):
                    self.content.append(TypeToken(token, ASTT.C_enumequal))
                    self.swap_out()
                    kids = tuple(cursor.get_children())
                    if kids:
                        self.zones.append(Zone(Zone_Type.Enum_Equal, kids))
                    else:
                        self.zones.append(Zone(Zone_Type.Enum_Equal, (cursor,)))
                else:
                    # Variable / Struct Initializer expression zone
                    kids = tuple(cursor.get_children())
                    if kids:
                        self.zones.append(Zone(Zone_Type.Initializer_Expr, kids))
                    else:
                        self.zones.append(Zone(Zone_Type.Initializer_Expr, (cursor,)))

        return

    def exec_keyword(self, token, cursor):
        for zone in self.zones:
            if zone.check_exec(token, cursor, AST_KIND.keyword):
                return

        self.keyword_parse(token, cursor)
        return

    def exec_identifier(self, token, cursor):
        for zone in self.zones:
            if zone.check_exec(token, cursor, AST_KIND.identifier):
                return

        tspelling = getattr(token, 'spelling_str', '')
        # Ignore predefined identifiers in expression contexts
        if tspelling in ("__func__", "__FUNCTION__", "__PRETTY_FUNCTION__"):
            return

        if cursor.type.kind == cc.TypeKind.TYPEDEF:
            self.content.append(TypeToken(token, ASTT.C_SCtypedef))
            self.content.get_foreign(cursor)
            self.swap_out()
            return

        if self.content.content:
            if self.content.content[-1].type in (ASTT.C_struct, ASTT.C_union, ASTT.C_enum, ASTT.C_functionproto):
                self.name = tspelling
                self.content.append(TypeToken(token, self.content.content[-1].type))
                self.content.get_foreign(cursor)
                self.swap_out()
                return

        self.content.append(TypeToken(token))
        self.swap_out()
        return

    def exec_literal(self, token, cursor):
        for zone in self.zones:
            if zone.check_exec(token, cursor, AST_KIND.literal):
                return True

        self.content.append(TypeToken(token, ASTT.Undefined))
        return


