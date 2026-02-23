from globalstuff import *
from collections import namedtuple
from operator import itemgetter
from DBHandling import *
import types
import sys
from TypeList import type_check, t_dir, t_c, t_kconfig, t_rust
from parser.c_ast import c_ast_parse

class Referenced_Element:
	"""
	Get the values in prior item in array, works in tandem with GP.
	"""
	def __init__(self, offset=None, attribute=None):
		self.offset = offset
		self.stored_attribute = attribute

	def __setstate__(self, state):
		self.offset = state.get("offset", None)
		self.stored_attribute = state.get("stored_attribute", None) #Needed for unpickleling

	def __getitem__(self, key):
		return Referenced_Element(key)

	def __getattr__(self, name):
		return Referenced_Element(self.offset, name)

	def get_value(self, current_list):
		if self.offset < 0:
			output = current_list[(self.offset+len(current_list))]
		else:
			output = current_list[self.offset]
		if self.stored_attribute:
			output = getattr(output, self.stored_attribute)
		return output
	
	def __str__(self):
		return f"Referenced_Element({self.offset}, {self.stored_attribute})"


class Delayed_Executor:
	"""
	Preserves the values in set/update command, works in tandem with GP.
	"""
	def __init__(self, table, action, item):
		self.table = table
		self.action = action
		self.item = item

	def process(self, array):
		return getattr(getattr(sys.modules["__main__"], self.table), self.action)(self.item, array)

	def __str__(self):
		temp_str = ""
		for value in self.item:
			temp_str += f"{value},"
		return f"Delayed_Executor:table({self.table}),action({self.action}),item({temp_str[:-1]})"


class Change_Set:
	def __init__(self, operation, current_path, old_path = None):
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
		

	def __call__(self, item):
		#print(f"{self.cs} + {item}")
		return self.cs.append(item)

	def store(self, name, item):
		if item is None:
			print(f"CS.Store failed (is None):{name}")
			raise MyBreak
		self.store_dict[name] = len(self.cs)
		return self(item)

	def get_ref(self, name, attribute=None):
		if self.cs[self.store_dict[name]].__class__.__name__ == "Delayed_Executor":
			return Referenced_Element(self.store_dict[name], attribute)
		if attribute:
			return getattr(self.cs[self.store_dict[name]], attribute)
		return self.cs[self.store_dict[name]]

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
	def __init__(self, table_name: str, columns: tuple, primary: tuple, foreign=None, initial_insert=None, no_duplicate=False, select_procedure=False):
		# Name of table as a "String"
		self.table_name = table_name
		# Columns with arguments in a tuple: 
		# (("col1", "INT", "NOT NULL", "AUTO_INCREMENT"),("col2", "INT", "NOT NULL", "AUTO_INCREMENT"))
		self.init_columns = columns
		# Name of the primary key as a "String"
		self.init_primary = primary
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

		return


	class Column_Class:
		def __init__(self, table_name: str, column_id: int):
			self.table_name = table_name
			self.column_id = column_id
			return

		def __call__(self, value=None):
			return (self.table_name, self.column_id, value)

	def create_table(self):
		if OVERRIDE_TABLE_CREATION_PRINT:
			print(f"Creating {table_name}")
		
		self.reference_name = {}
		self.auto_increment = False
		self.get_list = False
		temp_id = 0
		temp_arr = []
		temp_sql = ""
		for col in self.init_columns:
			self.reference_name[col[0]] = temp_id
			setattr(self, f"{col[0]}", self.Column_Class(self.table_name, temp_id))
			temp_arr.append(col[0])

			if "AUTO_INCREMENT" in col:
				self.auto_increment = True

			temp_sql += f'{" ".join(map(str, col))},'

			temp_id += 1

		self.namedtuple = namedtuple(self.table_name, temp_arr)
		self.namedtuple.__qualname__ = f"{self.table_name}.namedtuple" #Needed for pickleling
		self.namedtuple.__module__= "__main__" #Needed for pickleling
		self.columns = tuple(temp_arr)

		temp_arr_primary = []
		temp_sql += " PRIMARY KEY (" 
		for keys in self.init_primary:
			temp_sql += f"{keys},"
			temp_arr_primary.append(temp_arr.index(keys))

		self.primary_key = tuple(temp_arr_primary)
		del temp_arr, temp_arr_primary


		self.sql_set = f'INSERT INTO {self.table_name} VALUES ({("%s," * len(self.columns))[:-1]})'
		key_update_sql = ""

		if OVERRIDE_TABLE_CREATION_PRINT:
			print("primary_key:")
			print(self.primary_key)
			print("columns:")
			print(self.columns)
			

		for column in self.columns:
			if column not in itemgetter(*self.primary_key)(self.columns):
				key_update_sql += f"{column} = VALUES({column}), "
		self.sql_update = f'INSERT INTO {self.table_name} ({", ".join(map(str, self.columns))}) VALUES ({("%s," * len(self.columns))[:-1]}) ON DUPLICATE KEY UPDATE {key_update_sql[:-2]}'


		temp_sql = f"{temp_sql[:-1]}),"

		if self.init_foreign:
			for keys in self.init_foreign:
				temp_sql += f" FOREIGN KEY ({keys[0]}) REFERENCES {keys[1]}({keys[2]}),"

		db = set_db()
		if OVERRIDE_TABLE_CREATION_PRINT:
			print(f"SQL COMMAND: CREATE TABLE {self.table_name} ({temp_sql[:-1]} );")
			print("-----------------")
		execdb(db, f"CREATE TABLE {self.table_name} ({temp_sql[:-1]} );")
		del temp_sql

		if self.initial_insert:
			execdb(db, f'INSERT INTO {self.table_name} VALUES ({("%s," * temp_id)[:-1]})', self.initial_insert)
		del temp_id

		unset_db(db)
		return


	def clear_fetch(self, gp_instance):
		self.optimized_table = {}
		self.optimized_table_list = {}
		self.current_table = {}
		self.set_table = {}
		self.update_table = {}

		if self.select_procedure is False:
			return

		if isinstance(self.select_procedure, types.LambdaType):
			select_command = self.select_procedure(gp_instance)
		else:
			select_command = f"SELECT * FROM {self.table_name}"

		db = set_db()
		selectdb(db, select_command)

		for row in db[1].fetchall():
			self.current_table[itemgetter(*self.primary_key)(row)] = self.namedtuple(*row)

		if self.auto_increment:
			self.no_duplicate_dict = {}
			selectdb(db, f"SELECT COALESCE(MAX({self.columns[0]}),0) FROM {self.table_name};")
			current_max_id = db[1].fetchone()
			self.set_index = current_max_id[0] + 1


		unset_db(db)
		return

	# Will create an optimized table that returns a list instead of an individual tuple
	def gen_optimized_table_list(self, *columns):
		self.get_list = True
		return gen_optimized_table(*columns)

	def gen_optimized_table(self, *columns):
		key_group = tuple(map(itemgetter(1), columns))

		self.optimized_table[key_group] = {}

		if self.get_list:
			self.optimized_table_list[key_group] = True
			for row in self.current_table.values():
				if self.optimized_table[key_group].get(itemgetter(*key_group)(row)):
					self.optimized_table[key_group][itemgetter(*key_group)(row)].append(itemgetter(*self.primary_key)(row))
				else:
					self.optimized_table[key_group][itemgetter(*key_group)(row)] = list(itemgetter(*self.primary_key)(row))
		else:
			for row in self.current_table.values():
				self.optimized_table[key_group][itemgetter(*key_group)(row)] = itemgetter(*self.primary_key)(row)
		self.get_list = False
		return

	def get(self, *columns):
		if not columns:
			return None

		key_group = tuple(map(itemgetter(1), columns))
		values = tuple(map(itemgetter(2), columns))

		if len(values) == 1:
			values = values[0]

		if key_group == self.primary_key:
			return self.current_table.get(values)
		else:
			if key_group in self.optimized_table:
				if self.optimized_table_list.get(key_group):
					get_array = []
					if (keys := self.optimized_table[key_group].get(values)): # ruff F841 : we dont use keys, we could not assign it
						for key in self.optimized_table[key_group].get(values):
							if (query_result := self.current_table.get(key)):
								get_array.append(query_result)
						if get_array:
							return get_array
					return None
				else:
					return self.current_table.get(self.optimized_table[key_group].get(values))
			else:
				print(f"Error with: {columns}")
				print("Revert to for loop for get()")
				for row in self.current_table.values():
					if itemgetter(*key_group)(row) == values:
						return row
				return None

	def __call__(self, *item):
		return self.set(*item)

	def actual_set(self, *item):
		if type(item[0]) == tuple:
			item = item[0]

		# IF YOU HAVE AN ERROR LIKE(TypeError: m_file_name.__new__() takes 3 positional arguments but 4 were given) THAT MEANS THAT YOU SHOULD USE GET_SET
		item = self.namedtuple(*item)
		if (self.auto_increment) and (item[0] is None):
			if self.no_duplicate:
				if (no_dup_key := self.no_duplicate_dict.get(item[1:])) is not None:
					return self.set_table[no_dup_key]

				while self.set_index in self.set_table:
					self.set_index += 1

				item = (self.set_index,) + item[1:]
				self.no_duplicate_dict[item[1:]] = self.set_index
				self.set_index += 1

			else:
				while self.set_index in self.set_table:
					self.set_index += 1

				item = (self.set_index,) + item[1:]
				self.set_index += 1
		else:
			if itemgetter(*self.primary_key)(item) in self.set_table:
				return self.set_table[itemgetter(*self.primary_key)(item)]

		self.set_table[itemgetter(*self.primary_key)(item)] = self.namedtuple(*item)
		return self.namedtuple(*item)

	def set(self, *item):
		return Delayed_Executor(self.table_name, "dset", item)

	def dset(self, items, array):
		output = []
		for item in items:
			#print(f"{self.table_name}={item}")
			if item.__class__.__name__ == "Referenced_Element":
				output.append(item.get_value(array))
			else:
				output.append(item)
		return self.actual_set(*output)

	def get_set(self, *columns):
		temp = self.get(*columns)

		if temp is not None:
			return temp

		temp_arr = [None] * len(self.init_columns)
		for item in columns:
			temp_arr[item[1]] = item[2]
		return self.set(*temp_arr)

	def actual_get_set(self, *columns):
		temp = self.get(*columns)

		if temp is not None:
			return temp

		temp_arr = [None] * len(self.init_columns)
		for item in columns:
			temp_arr[item[1]] = item[2]
		return self.actual_set(*temp_arr)


	def actual_update(self, *item):
		if type(item[0]) == tuple:
			item = item[0]

		current_item = self.current_table.get(itemgetter(*self.primary_key)(item))
		new_item = []

		if current_item is None:
			return None

		for part_item in item:
			if part_item is None:
				part_item = current_item[len(new_item)]
			new_item.append(part_item)

		self.update_table[itemgetter(*self.primary_key)(new_item)] = self.namedtuple(*new_item)
		return self.namedtuple(*new_item)

	# CAN RETURN NONE IF NO ITEM AT PRIMARY KEY
	def update(self, *item):
		return Delayed_Executor(self.table_name, "dupdate", item)

		

	def dupdate(self, items, array):
		output = []
		for item in items:
			if item.__class__.__name__ == "Referenced_Element":
				output.append(item.get_value(array))
			else:
				output.append(item)
		return self.actual_update(*output)

	def insert_set(self):
		if not self.set_table:
			return
		db = set_db()
		try:
			execdb(db, self.sql_set, tuple(tuple(col) for col in self.set_table.values()))
		except Exception as e:
			print(f"Error happend while insert_set() in table:{self.table_name}")
			print(db[1].statement)
			print(e)
			emergency_shutdown(4)
		unset_db(db)
		del self.set_table
		self.set_table = {}
		return

	def insert_update(self):
		if not self.update_table:
			return
		db = set_db()
		try:
			key_update_sql = ""
			for column in self.reference_name:
				if column not in self.primary_key:
					key_update_sql += f"{column} = VALUES({column}), "
			execdb(db, self.sql_update, tuple(tuple(col) for col in self.update_table.values()))
		except Exception as e:
			print(f"Error happend while insert_update() in table:{self.table_name}")
			print(db[1].statement)
			print(e)
			emergency_shutdown(5)
		unset_db(db)
		del self.update_table
		self.update_table = {}
		return




