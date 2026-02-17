import sys
import shutil
from pathlib import Path

RAMDISK = "/dev/shm"
CPUS = 8
linux_directory = Path('linux')

CLEAN_PRINT = True

#FUNCTIONS
OVERRIDE_CPPRO_CINDEX_INPUT = True
#PRINTS
#	SQL
OVERRIDE_TABLE_CREATION_PRINT = False
#	AST
OVERRIDE_C_AST_LINE_PRINT = True
OVERRIDE_CPPRO_PRINT = True
OVERRIDE_CINDEX_ERROR_PRINT = True
OVERRIDE_CINDEX_SKIPPED_PRINT = True
#	GIT
OVERRIDE_FORGOTTEN_PRINT = False
#	General Print
OVERRIDE_MAX_PRINT_SIZE = 60
class MyBreak(Exception): pass


def green(string_arg):
	return f"\033[92m{string_arg}\033[0m"
def red(string_arg):
	return f"\033[93m{string_arg}\033[0m"
def magenta(string_arg):
	return f"\033[35m{string_arg}\033[0m"
def cyan(string_arg):
	return f"\033[36m{string_arg}\033[0m"

def emergency_shutdown(number_error=1):
	for directory in sys.modules["__main__"].gp.PURGE_LIST:
		try:
			shutil.rmtree(directory)
		except Exception:
			pass
	sys.exit(number_error)
	return

def BP():
	print("=====BREAKPOINT=====\nc: Continue execution\nq: Quit the debugger\nn: Step to the next line within the same function\ns: Step to the next line in this function or a called function.")
	sys.breakpointhook()
	return

def String_shortner(text,mode="boxed+indent"):
	formated_text = ""
	wrapped_text = wrap_lines([text],OVERRIDE_MAX_PRINT_SIZE+(OVERRIDE_MAX_PRINT_SIZE/2))

	if mode == "boxed":
		formated_list = render_ansi_box(wrapped_text)
	elif mode == "indent":
		formated_list = render_with_indent(wrapped_text, "    > ")
	elif mode == "boxed+indent":
		formated_list = render_ansi_box(render_with_indent(wrapped_text, "    > "))

	for line in formated_list[0]:
		formated_text = f"{formated_text}\n{line}"
	return formated_text




