from globalstuff import *
from pathlib import Path
import re
import clang.cindex as cc
import ctypes
import types
import json

def serializer(obj):
	return obj.__dict__


def G(name):
	return getattr(sys.modules["__main__"], name)

def c_ast_parse(CS):
	match CS.operation:
		case "A":
			c_ast_parse_add(CS)
		case _:
			print("this shit is not implemented")
	return

def c_ast_parse_add(CS):

	AM = Ast_Manager(CS)

	for cpp_element in AM.cppro_parse_result:
		cpp_element.extract(CS)

	for processed_element in AM.processing_list:
		processed_element.extract(CS)

	return



class CXSourceRangeList(ctypes.Structure):
	_fields_ = [("count", ctypes.c_uint),("ranges", ctypes.POINTER(cc.SourceRange))]

CXSourceRangeList_P = ctypes.POINTER(CXSourceRangeList)

def good_looking_printing(object_name, pre_result = "", post_result = " "):
	result = " "
	multi_line_leap = False
	list_wait_arr = []
	for key in vars(object_name):
		if not getattr(object_name, key):
			continue
		if (isinstance(getattr(object_name, key), list) or (isinstance(getattr(object_name, key), tuple))):
			list_wait_arr.append(key)
		else:
			to_be_added = f"{magenta(key)}:{getattr(object_name, key)},"
			if len(result.splitlines()[-1]) > OVERRIDE_MAX_PRINT_SIZE:
				if not multi_line_leap:
					pre_result += "\n"
					result = f"   {result}"
					multi_line_leap = True
				to_be_added = f"\n{to_be_added}"
			result += to_be_added
	result = result[:-1] # comma remover

	if multi_line_leap:
		result = result.replace("\n", "\n   ")

	for key in list_wait_arr:
		#if multi_line_leap:
			#result += "   "
		result += f", {green(key)}" + green(": {")
		for key_key in getattr(object_name, key):
			result += "\n   "
			result += str(key_key).replace("\n", "\n   ")
		result += green("\n}")
	return f"{pre_result}{result[1:]}{post_result}"

########https://gist.github.com/ChunMinChang/88bfa5842396c1fbbc5b
def commentRemover(text):
	def replacer(match):
		s = match.group(0)
		if s.startswith('/'):
			return "\n" * s.count( "\n" )
		else:
			return s
	pattern = re.compile(
		r'//.*?$|/\*.*?\*/|\'(?:\\.|[^\\\'])*\'|"(?:\\.|[^\\"])*"',
		re.DOTALL | re.MULTILINE
	)
	return re.sub(pattern, replacer, text)
#######


class Line:
	def __init__(self, *args):
		match len(args):
			case 1:
				if isinstance(args[0], cc.SourceRange):
					self.line_pos = (args[0].start.line, args[0].end.line)
					self.char_pos = (args[0].start.column, args[0].end.column)
				else:
					print("Line: 1 ARGS TYPE ERROR")
			case 2:
				self.line_pos = (args[0], args[1])
				self.char_pos = (0, 0)
			case 4:
				self.line_pos = (args[0], args[1])
				self.char_pos = (args[2], args[3])

	# Code Capture
	def cc(self, rawfile):
		split_rawfile = rawfile.splitlines()

		#Line Select
		try:
			if self.line_pos[0] == self.line_pos[1]:
				self.code = split_rawfile[self.line_pos[0]-1]
			else:
				self.code = "\n".join(split_rawfile[self.line_pos[0]-1:self.line_pos[1]])
		except IndexError:
			self.code = None
			return self


		if self.char_pos[0] == 0:
			char_start = 0
		else:
			char_start = self.char_pos[0] - 1

		if self.char_pos[1] == 0:
			self.code = self.code[char_start:]
		else:
			char_end = self.char_pos[1] - 1

			#  Char Trim
			try:
				self.code = self.code[char_start:(char_end - len(split_rawfile[self.line_pos[1]-1]))]
			except IndexError:
				self.code = None
		return self

	def __str__(self):
		if (self.line_pos == (0,0)) and (self.char_pos == (0,0)):
			return "None"

		if OVERRIDE_C_AST_LINE_PRINT:
			if "code" in vars(self):
				return f"(S{self.line_pos[0]}[{self.char_pos[0]}], E{self.line_pos[1]}[{self.char_pos[1]}], C­<{self.code}>)"

		return f"(S{self.line_pos[0]}[{self.char_pos[0]}], E{self.line_pos[1]}[{self.char_pos[1]}])"

class Ast:
	def ast_debug(self, CS, ast_id_name):
		CS.store(f"m_ast_debug{len(CS.cs)}", G("m_ast_debug")(
			CS.get_ref(ast_id_name, "ast_id"),
			json.dumps(self.__dict__, default=serializer)
		))
		return

	def tag(self, CS, ast_id_name, line = None):
		self.line = line
		if self.line is None:
			self.line = Line(0,0)

		# Create tag
		CS.store(f"m_tag{len(CS.cs)}", G("m_tag")(
			None,
			CS.current_vid,
			0,
			"",
			CS.get_ref(ast_id_name, "ast_id"),
			0,
			0
		))

		# Create bridge tag
		CS.store(f"m_bridge_tag{len(CS.cs)}", G("m_bridge_tag")(
			CS.get_ref("file", "fid"),
			CS.get_ref(f"m_tag{len(CS.cs)-1}", "tag_id"),
			self.line.line_pos[0],
			self.line.line_pos[1]
		))
		return

	def extract(self, CS):
		ast_id_pos = len(CS.cs)
		# Create ast
		CS.store(f"AST{ast_id_pos}", G("m_ast")(
			None,
			f"AST{len(CS.cs)}",
			0
		))

		# Create ast_debug
		self.ast_debug(CS, f"AST{ast_id_pos}")

		# Create tag and bridge_tag
		self.tag(CS, f"AST{ast_id_pos}")

		return

	def extract_1arg(self, CS, type_id, arg, line = None):
		ast_id_pos = len(CS.cs)
		# Create ast
		CS.store(f"AST{ast_id_pos}", G("m_ast").get_set(
			G("m_ast").name(arg),
			G("m_ast").type_id(type_id)
		))

		# Create ast_debug
		if OVERRIDE_FORCE_AST_DEBUG:
			self.ast_debug(CS, f"AST{ast_id_pos}")

		# Create tag and bridge_tag
		self.tag(CS, f"AST{ast_id_pos}", line)
		return


	def __str__(self):
		return good_looking_printing(self, red(f"\n{type(self).__name__}: "))

# RANGE 100 is reserved for CPPro
# type_id 100
class CPPro_ifdef(Ast):
	def __init__(self, line, identifier):
		self.line = line
		self.identifier = identifier

	def extract(self, CS):
		self.extract_1arg(CS, 100, self.identifier, self.line)
		return

# type_id 101
class CPPro_ifndef(Ast):
	def __init__(self, line, identifier):
		self.line = line
		self.identifier = identifier

	def extract(self, CS):
		self.extract_1arg(CS, 101, self.identifier, self.line)
		return

# type_id 102
class CPPro_if(Ast):
	def __init__(self, line, expression):
		self.line = line
		self.expression = expression

	def extract(self, CS):
		self.extract_1arg(CS, 102, self.expression, self.line)
		return

# type_id 103
class CPPro_elif(Ast):
	def __init__(self, line, expression):
		self.line = line
		self.expression = expression

	def extract(self, CS):
		self.extract_1arg(CS, 103, self.expression, self.line)
		return

# type_id 104
class CPPro_else(Ast):
	def __init__(self, line):
		self.line = line

	def extract(self, CS):
		self.extract_1arg(CS, 104, "", self.line)
		return

# type_id 105
class CPPro_endif(Ast):
	def __init__(self, line):
		self.line = line

	def extract(self, CS):
		self.extract_1arg(CS, 105, "", self.line)
		return

###############################
# type_id 106
class CPPro_define(Ast):
	def __init__(self, line, identifier, replacement):
		self.line = line
		self.identifier = identifier
		self.replacement = replacement

	def extract(self, CS):
		if self.replacement is None:
			# Empty define
			self.extract_1arg(CS, 9999, self.identifier, self.line)
			return

		# BAD IMPLEMENTATION, NEEDS TO BE FIXED, WE NEED RECURSIVE DETECTION FOR 2ND ARG
		self.extract_1arg(CS, 9998, self.identifier, self.line)

		return

# type_id 107
class CPPro_undef(Ast):
	def __init__(self, line, identifier):
		self.line = line
		self.identifier = identifier

	def extract(self, CS):
		self.extract_1arg(CS, 107, self.identifier, self.line)
		return

##################
# type_id 108
class CPPro_include(Ast):
	def __init__(self, line, written_include, actual_include):
		self.line = line
		self.w_include = written_include
		self.a_include = actual_include

	def extract(self, CS):
		####################BROKEN
		self.extract_1arg(CS, 108, self.w_include, self.line)
		return

# type_id 109
class CPPro_line(Ast):
	def __init__(self, line, lineno, filename):
		self.line = line
		self.lineno = lineno
		self.filename = filename

	def extract(self, CS):
		if self.filename is None:
			self.extract_1arg(CS, 109, f"{self.lineno}", self.line)
		else:
			self.extract_1arg(CS, 109, f"{self.lineno} {self.filename}", self.line)
		return

# type_id 110
class CPPro_error(Ast):
	def __init__(self, line, error_msg):
		self.line = line
		self.error_msg = error_msg

	def extract(self, CS):
		self.extract_1arg(CS, 110, self.error_msg, self.line)
		return

# type_id 111
class CPPro_pragma(Ast):
	def __init__(self, line, pragma):
		self.line = line
		self.pragma = pragma

	def extract(self, CS):
		self.extract_1arg(CS, 111, self.pragma, self.line)
		return

class Ast_STRUCT_DECL(Ast):
	def __init__(self, line, name, children=None):
		self.line = line
		self.name = name
		self.children = children

class Ast_UNION_DECL(Ast):
	def __init__(self, line, name=None, children=None):
		if isinstance(line, Ast_STRUCT_DECL):
			self.line = line.line
			self.name = line.name
			self.children = line.children
		else:
			self.line = line
			self.name = name
			self.children = children

class Ast_FUNCTION_DECL(Ast):
	def __init__(self, line, name, ast_type):
		self.line = line
		self.name = name
		self.ast_type = ast_type

class Ast_VAR_DECL(Ast):
	def __init__(self, line, name, ast_type):
		self.line = line
		self.name = name
		self.ast_type = ast_type

class Ast_TYPEDEF_DECL(Ast):
	def __init__(self, line, name, ast_type):
		self.line = line
		self.name = name
		self.ast_type = ast_type


class Ast_ENUM_DECL(Ast):
	def __init__(self, line, name, enumerator_list):
		self.line = line
		self.name = name
		self.enum_list = enumerator_list

class Ast_MACRO_INSTANTIATION(Ast):
	def __init__(self, line, name, filename, args):
		self.line = line
		self.name = name
		self.filename = filename
		self.args = args



class Ast_Struct_FIELD_DECL(Ast):
	def __init__(self, line, name, ast_type):
		self.line = line
		self.name = name
		self.ast_type = ast_type
class Ast_Struct_STRUCT_DECL(Ast):
	def __init__(self, line, name, ast_type):
		self.line = line
		self.name = name
		self.ast_type = ast_type
		self.member = []





Ast_Type_Undefine, Ast_Type_Pure, Ast_Type_Typedef, Ast_Type_Struct, Ast_Type_Function = range(5)
class Ast_Type():
	def __init__(self):
		self.type_style = Ast_Type_Undefine
		self.pointer = False
		self.pointer_const = False
		self.const = False
		self.debug = None
		# Ast_Type_Pure
		self.pure_kind = None
		# Ast_Type_Function
		self.func_type = None
		self.func_args = []
		self.func_args_name = []
		self.array = None

		# Ast_Type_Pure, Ast_Type_Typedef, Ast_Type_Struct, Ast_Type_Function
		self.type_name = None
		self.location_file = None
		self.location_line = None

	def __str__(self):
		pre_result = ""
		result = " "
		multi_line_leap = False
		list_wait_arr = []

		if self.type_style == Ast_Type_Pure:
			pre_result += cyan("Ast_Type_Pure(")
		elif self.type_style == Ast_Type_Typedef:
			pre_result += cyan("Ast_Type_Typedef(")
		elif self.type_style == Ast_Type_Struct:
			pre_result += cyan("Ast_Type_Struct(")
		elif self.type_style == Ast_Type_Function:
			pre_result += cyan("Ast_Type_Function(")
		else:
			#self.type_style == Ast_Type_Undefine:
			return red("Ast_Type_Undefine()")

		if True:
			return good_looking_printing(self, pre_result, cyan(")"))

		for key in vars(self):
			if isinstance(getattr(self, key), list):
				list_wait_arr.append(key)


			if getattr(self, key):
				to_be_added = f"{magenta(key)}:{getattr(self, key)},"
				if not multi_line_leap:
					pre_result += "\n"
					result = f"   {result}"
					multi_line_leap = True
				if len(result.splitlines()[-1]) > OVERRIDE_MAX_PRINT_SIZE:

					to_be_added = f"\n{to_be_added}"
				result += to_be_added

		if multi_line_leap:
			result = result.replace("\n", "\n   ")


		return f"{pre_result}{result[1:-1]}{cyan(')')}\n"

class Ast_Manager():
	def __init__(self, CS):
		self.mfdir = CS.mf.version_dict[CS.gp.Version_Name]
		self.filename = CS.current_path
		self.fullfilename = f"{self.mfdir}/{self.filename}"
		self.rawfile = Path(self.fullfilename).read_text(encoding='latin-1')
		self.processing_list = []
		self.cppro_parse_result = []
		self.Init_Parse()


	def cppro_line_parse(self, current_file, current_line, file_path):
		#This try: is a check for misformed CPPro tags like "#error"<-Without anything else....
		try:
			working_line = current_file[current_line].lstrip()

			loopval = 0
			if working_line == "":
				return
			if working_line[0] != '#':
				return

			# Start Handling possible " " or \t after #
			try:
				working_line = working_line[0] + working_line[1:].lstrip()
			except IndexError:
				return
			# End Handling possible " " or \t after #

			# Start \newline handling
			while current_file[current_line+loopval][-1] == "\\":
				# Start Confirm that there is a next line
				try:
					current_file[current_line+loopval+1]
				except IndexError:
					break
				# End Confirm that there is a next line

				loopval += 1
				if (current_file[current_line+loopval][0] == " ") or (current_file[current_line+loopval][0] == "\t"):
					working_line = working_line[:-1] + " \n" + current_file[current_line+loopval].lstrip()
				else:
					working_line = working_line[:-1] + "\n" + current_file[current_line+loopval]
			# End \newline handling

			match working_line.split(maxsplit=1)[0]:

				# Start #ifdef
				case "#ifdef":
					return CPPro_ifdef(Line(current_line+1, current_line+1+loopval).cc(self.rawfile), working_line[6:].lstrip())
				# End #ifdef

				# Start #ifndef
				case "#ifndef":
					return CPPro_ifndef(Line(current_line+1, current_line+1+loopval).cc(self.rawfile), working_line[7:].lstrip())
				# End #ifndef

				# Start #if
				case "#if":
					return CPPro_if(Line(current_line+1, current_line+1+loopval).cc(self.rawfile), working_line[3:].lstrip())
				# End #if

				# Start #elifndef AND #elifdef
				case "#elifndef":
					if OVERRIDE_GLOBAL_C_AST:
						print (f"SOME RETARDED DEVS, ADDED THIS FUCKING BULSHIT TO THEIR CODE: #elifndef , Line:{current_line+1}")
					emergency_shutdown(6)
				case "#elifdef":
					if OVERRIDE_GLOBAL_C_AST:
						print (f"SOME RETARDED DEVS, ADDED THIS FUCKING BULSHIT TO THEIR CODE: #elifdef , Line:{current_line+1}")
					emergency_shutdown(7)
				# End #elifndef AND #elifdef


				# Start #elif
				case "#elif":
					return CPPro_elif(Line(current_line+1, current_line+1+loopval).cc(self.rawfile), working_line[5:].lstrip())
				# End #elif

				# Start #else
				case "#else":
					return CPPro_else(Line(current_line+1, current_line+1+loopval).cc(self.rawfile))
				# End #else

				# Start #endif
				case "#endif":
					return CPPro_endif(Line(current_line+1, current_line+1+loopval).cc(self.rawfile))
				# End #endif

				# Start #define
				case "#define":
					working_line = working_line[7:].lstrip()
					parentheses = 0
					bypass = False
					arg_one = ""
					arg_two = ""
					for line_char in working_line:
						if not bypass:
							if line_char == "\n":
								continue
							if line_char == '(':
								parentheses += 1
							elif (line_char == ' ') or (line_char == '\t'):
								if parentheses == 0:
									bypass = True
							elif line_char == ')':
								parentheses -= 1
								if parentheses == 0:
									bypass = True

						if bypass:
							arg_two += line_char
						else:
							arg_one += line_char

					arg_two = arg_two.lstrip()
					if arg_two == "":
						arg_two = None

					return CPPro_define(Line(current_line+1, current_line+1+loopval).cc(self.rawfile), arg_one, arg_two)
				# End #define

				# Start #undef
				case "#undef":
					return CPPro_undef(Line(current_line+1, current_line+1+loopval).cc(self.rawfile), working_line[6:].lstrip())
				# End #undef

				# Start #include
				case "#include":
					working_line = working_line[8:].lstrip()
					if working_line == "":
						return CPPro_include(Line(current_line+1, current_line+1+loopval).cc(self.rawfile), "", "")

					if working_line[0] == "\"":
						written_include = "\""
						actual_path = f"{Path(file_path).parent}/"
						for line_char in working_line[1:]:
							written_include += line_char
							if line_char == "\"":
								break
					elif working_line[0] == "<":
						written_include = "<"
						actual_path = "include/"
						for line_char in working_line[1:]:
							written_include += line_char
							if line_char == ">":
								break
					else:
						return CPPro_include(Line(current_line+1, current_line+1+loopval).cc(self.rawfile), "", "")

					# PARSE THE ACTUAL INCLUDE
					written_include[1:-2]

					path_arr = []
					dotdot = 0
					# IT WILL FUCKING BREAK IF SOME RETARD PUT SOME ROOT PATH IN THERE LIBS, WHY WOULD SOMEONE DO SOMETHING SO WRONG???? WHO THE FUCJK KNOWS!!!! BEWARE
					for chunk in str(actual_path + written_include[1:-1]).split("/")[::-1]:
						if chunk == "..":
							dotdot += 1
						elif dotdot > 0:
							dotdot -= 1
						else:
							path_arr.append(chunk)

					return CPPro_include(Line(current_line+1, current_line+1+loopval).cc(self.rawfile), written_include, "/".join(path_arr[::-1]))
				# End #include

				# Start #line 453 /path
				case "#line":
					line_in_work = working_line[5:].lstrip()
					lineno = re.match(r'^\d+', line_in_work).group(1)
					print(current_file)
					print(f"this is a #line ({line_in_work}) ({lineno})")

					try:
						filename = line_in_work[len(lineno):].lstrip()
					except IndexError:
						filename = None

					return CPPro_line(Line(current_line+1, current_line+1+loopval).cc(self.rawfile), int(lineno), filename)
				# End #line

				# Start #error
				case "#error":
					return CPPro_error(Line(current_line+1, current_line+1+loopval).cc(self.rawfile), working_line[6:].lstrip().rstrip())
				# End #error

				# Start #pragma
				case "#pragma":
					return CPPro_pragma(Line(current_line+1, current_line+1+loopval).cc(self.rawfile), working_line[7:].lstrip())
				# End #pragma

		except IndexError:
			return

		return

	def cppro_parse(self, current_file, file_path):
		# Cleanup
		current_file = commentRemover(current_file).splitlines()

		result_arr = []
		bypass_num = 0
		for shit in range(len(current_file)):
			if shit<bypass_num:
				continue
			result = self.cppro_line_parse(current_file, shit, file_path)
			if result:
				if (temp := getattr(result, "line")) is not None:
					bypass_num = temp.line_pos[0]
				result_arr.append(result)
		return result_arr

	def ast_parse_function(self, c_children, ast_t=None):
		if ast_t is None:
			ast_t = Ast_Type()
		ast_t.type_style = Ast_Type_Function

		for kids in c_children.get_children():
			match kids.kind:
				case cc.CursorKind.COMPOUND_STMT:
					continue
				case cc.CursorKind.TYPE_REF:
					ast_t.func_type = self.ast_type_getter(kids)
					continue
				case cc.CursorKind.PARM_DECL:
					ast_t.func_args.append(self.ast_type_getter(kids))
					if kids.spelling != "":
						ast_t.func_args_name.append(kids.spelling)
					continue


		return ast_t

	def ast_type_getter(self, c_children, ast_t=None, bypass_type=None):
		if ast_t is None:
			ast_t = Ast_Type()

		if bypass_type:
			c_children_type = bypass_type
		else:
			c_children_type = c_children.type

		#####################################THIS IS FUCKED, PLS FIX, THANKS
		fucked = []
		for x in range(c_children.extent.start.line,c_children.extent.end.line+1):
			if self.diag_dict.get(x):
				fucked.append(str(self.diag[self.diag_dict[x]]))
		ast_t.debug = tuple(map(lambda x: x[x.find(" ")+1:], fucked))

		### this  ALSO DOESN'T WORK WHEN ARRAY CONTAINS SOME RANDOM SHIT IN IT THAT IS NOT A NUMBER
		### THIS SHIT PROBABLY NEEDS TO BE PART OF THE LOOP, good fucking job
		array_count = c_children_type.get_array_size()
		if array_count != -1:
			ast_t.array = array_count
			c_children_type = c_children_type.get_array_element_type()

		if c_children.spelling == "stack":
			#BP()
			pass

		try:
			if fucked:
				#FUCKING ARRAY TEST
				fucjk_line = self.rawfile.splitlines()[c_children.extent.end.line-1][c_children.extent.end.column-1:].lstrip()

				if fucjk_line[0] == "[":
					ast_t.array = True
					ast_t.array_expr = fucjk_line[1:fucjk_line.find("]")]
		except IndexError:
			pass


		while ((c_children_type.kind == cc.TypeKind.ELABORATED) or (c_children_type.kind == cc.TypeKind.POINTER)):
			# START POINTER HANDLING
			if c_children_type.kind == cc.TypeKind.POINTER:
				c_children_type = c_children_type.get_pointee()
				ast_t.pointer = True

				if c_children_type.is_const_qualified():
					ast_t.pointer_const = True
			# END POINTER HANDLING

			if c_children_type.kind == cc.TypeKind.ELABORATED:
				c_children_type = c_children_type.get_named_type()

		if c_children_type.is_const_qualified():
			ast_t.const = True


		match c_children_type.kind:
			case cc.TypeKind.FUNCTIONPROTO:
				ast_t.type_style = Ast_Type_Function
				ast_t = self.ast_parse_function(c_children, ast_t)
			case cc.TypeKind.RECORD:
				#struct and union, this shit will break....
				ast_t.type_style = Ast_Type_Struct
			case cc.TypeKind.TYPEDEF:
				ast_t.type_style = Ast_Type_Typedef
			case _:
				ast_t.type_style = Ast_Type_Pure
				ast_t.pure_kind = f"{c_children_type.kind}"

		ast_t.type_name = f"{c_children_type.spelling}"
		try:
			ast_t.location_file = f"{c_children_type.get_declaration().extent.start.file}"[len(self.mfdir):]
		except IndexError:
				return
		ast_t.location_line = Line(c_children_type.get_declaration().extent)

		return ast_t


	def ast_parse_struct_decl(self, c_children):
		children = []
		for member_decl in c_children.get_children():

			ast_t = self.ast_type_getter(member_decl)

			if ast_t.pointer:
				member_decl_type = member_decl.type.get_pointee()
			else:
				member_decl_type = member_decl.type


			# START CHECK FOR STRUCT MEMBER WITHIN
			if children:
				if children[-1].__class__.__name__ == "Ast_Struct_STRUCT_DECL":
					if f"{member_decl_type.get_declaration().spelling}" == children[-1].name:
						#NAME OF WHATEVER THE FUCK + INFO
						children[-1].member.append(member_decl.spelling)
						continue
			# END CHECK FOR STRUCT MEMBER WITHIN

			#print(f"   {member_decl.kind}---{member_decl.spelling}---{member_decl_type.get_declaration().spelling}")


			if cc.CursorKind.STRUCT_DECL == member_decl.kind:
				children.append(Ast_Struct_STRUCT_DECL(
					Line(member_decl.extent).cc(self.rawfile),
					member_decl.spelling,
					ast_t
				))
			elif cc.CursorKind.FIELD_DECL == member_decl.kind:
				children.append(Ast_Struct_FIELD_DECL(
					Line(member_decl.extent).cc(self.rawfile),
					member_decl.spelling,
					ast_t
				))

		if not children:
			children = None

		return Ast_STRUCT_DECL(
			Line(c_children.extent).cc(self.rawfile),
			c_children.spelling,
			children
		)


	def ast_parse_function_decl(self, c_children):
		return Ast_FUNCTION_DECL(Line(c_children.extent).cc(self.rawfile),
			c_children.spelling,
			self.ast_parse_function(c_children)
		)

	def ast_parse_var_decl(self, c_children):
		return Ast_VAR_DECL(Line(c_children.extent).cc(self.rawfile),
			c_children.spelling,
			self.ast_type_getter(c_children)
		)

	def ast_parse_enum_decl(self, c_children):
		enum_list = []
		for kids in c_children.get_children():
			if cc.CursorKind.ENUM_CONSTANT_DECL == kids.kind:
				enum_list.append(kids.spelling)

		return Ast_ENUM_DECL(Line(c_children.extent).cc(self.rawfile),
			c_children.spelling,
			tuple(enum_list)
		)

	def ast_parse_macro_instantiation(self, c_children):
		args = False
		name = c_children.spelling
		excluded_keywords = {"inline"}
		if name in excluded_keywords:
			return #SPECIAL OUTPUT

		filename = None
		if c_children.get_definition():
			filename = str(c_children.get_definition().extent.start.file)[len(self.mfdir)+1:]
			if filename == self.filename:
				filename = None

		if len(name) == (c_children.extent.end.column - c_children.extent.start.column):
			return Ast_MACRO_INSTANTIATION(
				Line(c_children.extent).cc(self.rawfile),
				c_children.spelling,
				filename,
				None
			)

		try:
			args = self.rawfile.splitlines()[c_children.extent.end.line-1][c_children.extent.start.column-1+len(name):c_children.extent.end.column-1]

		except IndexError:
			return Ast_MACRO_INSTANTIATION(
				Line(c_children.extent).cc(self.rawfile),
				c_children.spelling,
				filename,
				None
			)

		return Ast_MACRO_INSTANTIATION(
			Line(c_children.extent).cc(self.rawfile),
			c_children.spelling,
			filename,
			args
			)

	def ast_parse_typedef_decl(self, c_children):
		return Ast_TYPEDEF_DECL(
			Line(c_children.extent).cc(self.rawfile),
			c_children.spelling,
			self.ast_type_getter(c_children, None, c_children.underlying_typedef_type)
		)


	def ast_parse(self, c_children):
		#print(f"{c_children.kind}---{c_children.spelling}")
		match c_children.kind:
			case cc.CursorKind.STRUCT_DECL:
				return self.ast_parse_struct_decl(c_children)
			case cc.CursorKind.FUNCTION_DECL:
				return self.ast_parse_function_decl(c_children)
			case cc.CursorKind.VAR_DECL:
				return self.ast_parse_var_decl(c_children)
			case cc.CursorKind.ENUM_DECL:
				return self.ast_parse_enum_decl(c_children)
			case cc.CursorKind.UNION_DECL:
				return Ast_UNION_DECL(self.ast_parse_struct_decl(c_children))
			case cc.CursorKind.TYPEDEF_DECL:
				return self.ast_parse_typedef_decl(c_children)
			case cc.CursorKind.MACRO_INSTANTIATION:
				return self.ast_parse_macro_instantiation(c_children)
			case cc.CursorKind.INCLUSION_DIRECTIVE:
				return
			case cc.CursorKind.MACRO_DEFINITION:
				return
			case _:
				if OVERRIDE_GLOBAL_C_AST:
					print(f"{c_children.kind}---{c_children.spelling}")
		return

	def diag_process(self, diags):
		for x, diag in enumerate(diags):
			#itterrate iterate itterate iterrate enumarate
			if str(diag.location.file) == self.fullfilename:
				self.diag_dict[diag.location.line] = x

		return

	# include/linux/lockd/bind.h
	def Init_Parse(self):

		current_file = self.rawfile
		self.cppro_parse_result = self.cppro_parse(current_file, self.filename)

		cppro_cindex_input = []
		if OVERRIDE_CPPRO_CINDEX_INPUT:
			for ifdefs in filter(lambda x: x.__class__.__name__ == "CPPro_ifdef", self.cppro_parse_result):
				cppro_cindex_input.append(f"-D{ifdefs.identifier}")

		# Initialize the Clang index

		index = cc.Index.create()

		# these: "-M","-MG", were probably important, but who gives a shit as they print a bunch of shit on screen, lol
		translation_unit = index.parse(self.fullfilename, args=["-ferror-limit=0","-Wall",
			"-D__KERNEL__",*cppro_cindex_input,#"-nostdinc",
			f'-I{self.mfdir}/{"/".join(self.filename.split("/")[:-1])}',
			f"-I{self.mfdir}/include",
			f"-I{self.mfdir}/include/uapi"
		],
		options=(cc.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD+32768))
		#https://clang.llvm.org/doxygen/group__CINDEX__TRANSLATION__UNIT.html

		self.diag = tuple(translation_unit.diagnostics)
		self.diag_dict = {}
		self.diag_process(self.diag)

		#######################################################################
		cc.conf.lib.clang_getSkippedRanges.restype = CXSourceRangeList_P#
		#######################################################################
		Skipped_ranges = cc.conf.lib.clang_getSkippedRanges(
			translation_unit,
			translation_unit.get_file(self.fullfilename)
		)
		if OVERRIDE_CINDEX_SKIPPED_PRINT and OVERRIDE_GLOBAL_C_AST:
			print(green("=======Skipped_ranges.contents======="))
			temp_content = current_file.splitlines()
			for i in range(Skipped_ranges.contents.count):
				print(f"Skipped_range {i}: {Skipped_ranges.contents.ranges[i].start.file}")
				print(f"          Line:({Skipped_ranges.contents.ranges[i].start.line}, {Skipped_ranges.contents.ranges[i].end.line})")
				for content in temp_content[
					Skipped_ranges.contents.ranges[i].start.line-1:
					Skipped_ranges.contents.ranges[i].end.line
				]:
					print(f"   {content}")
			del temp_content

		self.processing_list = []
		for kids in translation_unit.cursor.get_children():
			if f"{kids.location.file}" == self.fullfilename:
				if (result := self.ast_parse(kids)):
					self.processing_list.append(result)

		if OVERRIDE_CPPRO_PRINT and OVERRIDE_GLOBAL_C_AST:
			print(green("=======Start CPPro Result======="))
			for cppro_elements in self.cppro_parse_result:
				print(f"{cppro_elements}")
			print(green("=======End CPPro Result======="))
		
		if OVERRIDE_GLOBAL_C_AST:
			print(green("======PRINT LOOP======"))
			# PRINT LOOP
			for x in self.processing_list:
				print(x)

		def type_of_warning(argx):
			match argx:
				case 0:
					return "Ignored"
				case 1:
					return "Note"
				case 2:
					return "Warning"
				case 3:
					return "Error"
				case 4:
					return "Fatal"
			return "WTF IS THIS"

		if OVERRIDE_CINDEX_ERROR_PRINT and OVERRIDE_GLOBAL_C_AST:
			print(green("=======Cindex Errors======="))
			if self.diag:
				print(red("Found Errors:"))
				for diag in self.diag:
					print(String_shortner(f"[{type_of_warning(diag.severity)}] - {diag.spelling} (Line:{diag.location.line} File:{diag.location.file})", "indent"))



			else:
				print("No Error Found")

		return


