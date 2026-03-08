from globalstuff import *
from collections import namedtuple
from DBHandling import *
import types
import sys
from TypeList import type_check, t_dir, t_c, t_kconfig, t_rust
from parser.c_ast import c_ast_parse

# How are the instructions encoded with Change_Set
# Change_Set is at its core just a list of operations
# The operations are held in Change_Set.cs[] and are encoded like so:
#	(1,			#ID of the table being affected
#	OP_SET,		#ID of the operation being done (nothing=0,set=1,update=2,ref=4)
#	(2,"s"))	#Data Tuple, contains the data for the operation
# When an operation is applied, it may contain references, 
# these references need to be resolved before doing the operation.
# The simple way that we detect references is the presence of a tuple
# inside the 'Data Tuple'.

# Here is how references are encoded:
#	(	(	1,			#ID of the table being affected
# 			0,			#Column that needs to be extracted from table
# 			False		#Toggle 'old' that allow us to reference the previous version of a table
#		),
#		OP_REF,			#Constant, shows that it is a reference, may be removed in the future
#		"attribute"		#The attribute allows us to pass along information
# 	)					#on what item we wish to reference, should be used with the code parsers



class Change_Set:
	def __init__(self, operation = None, current_path = None, old_path = None):
		self.operation = operation
		self.current_path = current_path
		self.current_vid = 0
		self.old_path = old_path
		self.cs = []
		self.cs_processed = False
		self.cs_result = []
		self.cs_result_dict = {}
		self.file = None
		self.store_dict = {}
		self.gp = None
		self.mf = None
		

	def ref_processing(self,ref):
		key = ref[0][0]
		if ref[0][2]:
			key = f"{key}old"
		if ref[2]:
			key = f"{key}{ref[2]}"

		return self.cs_result[self.store_dict[key]][ref[0][1]]


	def execute(self):
		if self.cs_processed:
			return True

		instruction_offset = len(self.cs_result)
		for y, instruction in enumerate(self.cs[instruction_offset:]):
			#For now there is no option that could not be processed, this design will need edit
			if type(instruction[2]) is tuple:
				data = tuple(map(
					lambda x: self.ref_processing(x) if type(x) is tuple else x,
					instruction[2])
				)
			else:
				print("Something very bad in tablehandling execute")

			if instruction[1] == OP_DONE:
				self.cs_result.append(data)
				continue
			if instruction[1] == OP_SET:
				self.cs_result.append(TE.set(instruction[0], data))
				continue
			if instruction[1] == OP_UPDATE:
				self.cs_result.append(TE.update(instruction[0], data))
				continue
			print(f"ERROR, UNKNOWN OPERATION{instruction}")
		
		self.cs_processed = True
		return True

	def store(self, item, attribute=None):
		if item is None:
			print(f"CS.Store failed (is None)")
			raise MyBreak

		if self.operation is None:
			return self.cs.append(item)

		if attribute:
			self.store_dict[f"{item[0]}{attribute}"] = len(self.cs)
		else:
			self.store_dict[item[0]] = len(self.cs)

		return self.cs.append(item)


	def get_ref(self, name, attribute=None):
		key = name[0]
		if name[2]:
			key = f"{name[0]}old"
		if attribute:
			key = f"{key}{attribute}"

		if (ref := self.cs[self.store_dict[key]])[1] == 0:
			return ref[2][name[1]]

		return (name, OP_REF, attribute)

	# Will select the right parser and execute it
	def parse(self):
		current_type = type_check(self.current_path)
		if current_type == t_c:
			c_ast_parse(self)
		else:
			pass
		return

	def clear_bloat(self):
		self.gp = None
		self.mf = None
		return

	def __str__(self):
		temp_str = ""
		for value in self.cs:
			temp_str += f"{value},"
		return f"CS:file({self.current_path}),op({self.operation}),cs({temp_str[:-1]})"

class Table:
	#example: table("time", (("tid", "INT", "NOT NULL", "AUTO_INCREMENT"),("vid_s", "INT", "NOT NULL"),("vid_e", "INT", "NOT NULL")), ("tid",), (("vid_s", "v_main", "vid"),("vid_e", "v_main", "vid")), (0, 0, 0) )
	def __init__(self, gpid, table_name: str, columns: tuple, primary: tuple, foreign=None, initial_insert=None, no_duplicate=False, select_procedure=False):
		# ID in gp.Table_array
		self.gpid = gpid
		# Name of table as a "String"
		self.table_name = table_name
		# Columns with arguments in a tuple: 
		# (("col1", "INT", "NOT NULL", "AUTO_INCREMENT"),("col2", "INT", "NOT NULL", "AUTO_INCREMENT"))
		self.init_columns = columns
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

		for x, column in enumerate(self.init_columns):
			setattr(self, column[0], (self.gpid, x, False))
			setattr(self, f"{column[0]}_old", (self.gpid, x, True))

		return

	def start_te(self):
		TE.start(self)
		return

	def set(self, *columns):
		if self.no_duplicate:
			return self.get_set(table, *columns)

		return (self.gpid, OP_SET, columns)

	def update(self, *columns):
		return (self.gpid, OP_UPDATE, columns)

	def get(self, *columns):
		result = TE.get(self.gpid, columns)
		if result is None:
			return None

		return (self.gpid, OP_DONE, result)

	def get_set(self, *columns):
		result = TE.get(self.gpid, columns)

		if result:
			return (self.gpid, OP_DONE, result)

		return (self.gpid, OP_SET, columns)
