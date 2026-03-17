from globalstuff import *
from collections import namedtuple
from DBHandling import *
import types
import sys
from TypeList import type_check, t_dir, t_c, t_kconfig, t_rust
from parser.c_ast import c_ast_parse
from operator import itemgetter


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


# Views are a thing i pulled out of my.... They are created for the AST tables which 
# requires uniqueness across multiple tables but doesn't on any single one. Here is an example:
# m_ast:			(ast_id, name, type_id) Here, name and type_id could be duplicated over multiple ast_ids
# m_ast_container:	(ast_id, priority, name, subtype, ref_ast_id) Same here, we can have duplicates...abs
# BUT
# If we put (join) both of them together, No duplicate are allowed. 
# This is where the "View" comes in.
# Now lets apply this commend to an actual View to make sense of it:
# m_ast.view(			
# 	(					#First arg is a list of tuple, each contain the columns used for the INNER JOIN
#		(m_ast.ast_id, m_ast_container.ast_id, 1), #We call it the Joins
#	),					#INNER JOIN m_ast_container ON m_ast.ast_id=m_ast_container.ast_id
###						#The last value of the tuple represent the repetition of said table
###						#If a Views is used for one table, the Joins should look like this: (m_ast.ast_id,)
#	OP_VIEW_SET			#Views can either be resolved(OP_VIEW_DONE)
###						#(meaning that we have all values for them and they are already in the DB)
###						#Or it can be waiting to be inserted(OP_VIEW_SET)
###						#KEEP IN MIND THAT YOU DON'T NEED TO ADD THE OP_VIEW_SET(OR OP_VIEW_DONE) IN YOU CODE,
###						#THIS IS TO ILLUSTRATE WHAT GETS STORED IN CS.cs
###
#	(					#Here we get in the data, the data is layed out in one tuple 
#		None, "MyStruct", 2, None, 1, "MagicMyStruct", 1, 100
#	)					#following the number of elements from the tables in the first arg
# )						#Here you should essentialy write in the same way you would for a set
#						#with auto increment ID, The view will set the IDs itself.
#						#Views also allow for references and for repetition of the same table CONCAT style


def contains_tuple(columns):
	for col in columns:
		if type(col) is tuple:
			return True

	return False


class Change_Set:
	def __init__(self, operation = None, current_path = None, old_path = None):
		self.operation = operation
		self.current_path = current_path
		self.current_vid = 0
		self.old_path = old_path
		self.cs = []
		self.cs_processed = False
		self.cs_result = []
		self.cs_result_view = {}
		self.cs_result_dict = {}
		self.file = None
		self.store_dict = {}
		self.gp = None
		self.mf = None
		

	def ref_processing(self, ref):
		if ref[1] == OP_REF:
			key = ref[0][0]
			if ref[0][2]:
				key = f"{key}old"
			if ref[2]:
				key = f"{key}{ref[2]}"
			return self.cs_result[self.store_dict[key]][ref[0][1]]

		if ref[1] == OP_POS_REF:
			return self.cs_result[ref[2]][ref[0][1]]

		if ref[1] == OP_VIEW_REF:
			query = ref[0]
			joins = self.cs[ref[2]]
			view_data = self.cs_result[ref[2]]

			if joins[0][0][0] == query[0]: 
				return view_data[query[1]]
				
			data_offset = len(gp.Table_Array[joins[0][0][0]].init_columns)
			
			for join in joins:
				for x in range(join[2]):
					if join[0][1] == query[0]:
						return view_data[data_offset+query[1]]
					data_offset += len(gp.Table_Array[join[1][0]].init_columns)

		if ref[1] == OP_DICT_REF:
			filename = ref[3]
			data = ref[2]
			query = ref[0]

			if filename is None:
				print("i have no fucking clue how we will go about this shit")
			
			#if self.gp.Change_Set_Dict.get(filename) is not None:
				#for y, operation in enumerate(self.gp.Change_Set_Dict[filename].cs_result):
					#self.gp.Change_Set_Dict[filename].cs[y]
					#if operation[0][0][0] == query[0]:
					#	if filter(lambda x, col: False if (col is None) or (col == operation[2][x]) else True, enumerate(data)):
					#		return operation[2][query[1]]
					#
					#if operation[0] == query[0]:
					#	if filter(lambda x, col: False if (col is None) or (col == operation[2][x]) else True, enumerate(data)):
					#		return operation[2][query[1]]
		
		
			#raise REF_NOT_RESOLVABLE


		emergency_shutdown(43)
		return


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
			if instruction[1] == OP_VIEW_DONE:
				self.cs_result.append(data)
				continue

			if instruction[1] == OP_SET:
				self.cs_result.append(TE.set(instruction[0], data))
				continue
			if instruction[1] == OP_UPDATE:
				self.cs_result.append(TE.update(instruction[0], data))
				continue
			if instruction[1] == OP_VIEW_SET:
				self.cs_result.append(TE.view_set(instruction[0], data))
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

		if item[1] == OP_VIEW_DONE or item[1] == OP_VIEW_SET:
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

	def get_ref_pos(self, query, view_result_len):
		if (self.cs[view_result_len][1] == OP_VIEW_DONE) or (self.cs[view_result_len][1] == OP_DONE):
			return self.cs[view_result_len][query[1]]
		
		return (query, OP_POS_REF, view_result_len)


	def get_ref_view(self, query, view_result_len):

		view_result = self.cs[view_result_len]

		if view_result[1] != OP_VIEW_DONE:
			return (query, OP_POS_REF, view_result_len)

		joins = view_result[0]

		if joins[0][0][0] == query[0]: 
			return view_result[2][query[1]]
				
		data_offset = len(gp.Table_Array[joins[0][0][0]].init_columns)
		
		for join in joins:
			for x in range(join[2]):
				if join[0][1] == query[0]:
					return view_result[2][data_offset+query[1]]
				data_offset += len(gp.Table_Array[join[1][0]].init_columns)

		return (query, OP_VIEW_REF, view_result_len)

	def dict_ref(self, query, data, filename=None):
		if filename is None:
			return (query, OP_DICT_REF, data, filename)

		if self.gp.Change_Set_Dict.get(filename) is not None:
			for operation in self.gp.Change_Set_Dict[filename].cs:
				if (operation[1] == OP_VIEW_DONE):
					if operation[0][0][0] == query[0]:
						if filter(lambda x, col: False if (col is None) or (col == operation[2][x]) else True, enumerate(data)):
							return operation[2][query[1]]
				if (operation[1] == OP_DONE):
					if operation[0] == query[0]:
						if filter(lambda x, col: False if (col is None) or (col == operation[2][x]) else True, enumerate(data)):
							return operation[2][query[1]]

		return (query, OP_DICT_REF, data, filename)

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

		# FIX FOR C_AST
		setattr(sys.modules["parser.c_ast"], self.table_name, self)

		return

	def start_te(self):
		TE.start(self)
		return

	def set(self, *columns):
		if self.no_duplicate:
			return self.get_set(table, *columns)

		return (self.gpid, OP_SET, columns)

	def update(self, *columns):
		# Makes sure no ref are in our data, if so, abort
		if contains_tuple(columns):
			print(f"An {self.table_name}.update was done with unresolved refs, This is unexpedted behavior. CRASH")
			print(columns)
			emergency_shutdown(55)
		
		if None not in columns:
			return (self.gpid, OP_UPDATE, columns)

		primary_values = itemgetter(*self.primary)(columns)
		primary_values = primary_values if type(primary_values) is tuple else (primary_values,)
		get_columns = primary_values + (None,)*(len(columns)-len(self.primary))

		get_result = TE.get(self.gpid, get_columns)

		columns = tuple(map(lambda x: x[1] if x[1] is not None else get_result[x[0]], enumerate(columns)))
		return (self.gpid, OP_UPDATE, columns)

	def get(self, *columns):
		# Makes sure no ref are in our data, if so, abort
		if contains_tuple(columns):
			print(f"An {self.table_name}.get was done with unresolved refs, This is unexpedted behavior. CRASH")
			print(columns)
			emergency_shutdown(55)
		
		result = TE.get(self.gpid, columns)
		if result is None:
			return None

		return (self.gpid, OP_DONE, result)

	def get_set(self, *columns):
		# Makes sure no ref are in our data, if so, skip
		if not contains_tuple(columns):
			result = TE.get(self.gpid, columns)
			#print(f"{self.table_name}.get result:{result}")

			if result:
				return (self.gpid, OP_DONE, result)

		return (self.gpid, OP_SET, columns)

	def view(self, joins, data):
		# removing OLD
		joins = tuple(map(lambda join: tuple(map(lambda col_ref: col_ref[:2] if type(col_ref) is tuple else col_ref, join)), joins))
		
		# Makes sure no ref are in our data, if so, abort
		if not contains_tuple(data):
			result = TE.view_get(joins, data)

			if result:
				return (joins, OP_VIEW_DONE, result)

		return (joins, OP_VIEW_SET, data)

	def view_get(self, joins, data):
		# removing OLD
		joins = tuple(map(lambda join: tuple(map(lambda col_ref: col_ref[:2] if type(col_ref) is tuple else col_ref, join)), joins))
		
		# Makes sure no ref are in our data, if so, None returned
		if not contains_tuple(data):
			result = TE.view_get(joins, data)

			if result:
				return (joins, OP_VIEW_DONE, result)
		
		return None