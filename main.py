from globalstuff import *

import types
from collections import namedtuple
import mysql.connector
from operator import itemgetter
import subprocess as sp
import sys
import shutil
from pathlib import Path
import re
import pickle
import multiprocessing
import os
import argparse
from StringWrangler import wrap_lines, render_ansi_box, render_with_indent
from parser.c_ast import *
# Right now this only mutes (legacy) include errors that don't break everything


#    =========>enumerate()<=========
# THINGS THAT WE NEED TO SOLVE
# GCC COMPILE ARGS DETECTION __attribute__

# How we want to process a version
# 1. Get a list of changed files (including modified, deleted, moved and created)
# 2. Divide the task into multiple process for parsing.
# 3. (multicore) Parse the individual files (keep in mind about the possibility of not needing parsing like R100)
# 4. (multicore) Get the history of said files (query what is already existing for that file)
# 5. (multicore) Create updated list of what need to be changed in the DB
# 6. Recombine all the list
# 7. Do a final check for the file that might be pointing to another file's ast.
# 8. Send to DB




class _MyBreak(Exception): pass


########### DB UTILS ###########
def connect_sql():
	if os.path.exists('/.dockerenv') or 'docker' in open('/proc/self/cgroup').read():
		mysql_host = "host.docker.internal"
	else :
		mysql_host = "localhost"
	# Connect to DB
	return mysql.connector.connect(host=mysql_host, user="root", password="Passe123", database="test")

def set_db():
	db = []
	db.append(connect_sql())
	db.append(db[0].cursor())
	execdb(db, "SET SESSION sql_mode = 'NO_AUTO_VALUE_ON_ZERO';")
	execdb(db, "SET GLOBAL max_allowed_packet = 1073741824;")
	return db

def unset_db(db):
	db[1].close()
	db[0].close()
	return []

def execdb(db, sql, data=None):
	if data:
		if type(data[0]) is tuple:
			db[1].executemany(sql, data)
			return db[0].commit()
	db[1].execute(sql, data)
	return db[0].commit()

def selectdb(db, sql):
	db[1].execute(sql)
	return
########### DB UTILS ###########
class Change_Set:
	def __init__(self, file_name):
		self.file_name = file_name
		self.cs = []
		self.cs_processed = False
		self.cs_result = []
		self.cs_result_dict = {}
		self.includes = []
		self.tags = []
		self.tags_processed = False
	def __call__(self, *item):
		return self.cs.append(*item)

class Great_Processor:
	def __init__(self):
		global multi_proc
		multi_proc = False
		self.vid = 0
		self.loggin = []
		self.version_name = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
		self.vid = 0
		self.manager = None
		self.shared_set_list = None
		self.main_dict = {}

	def __getstate__(self):
		#useless
		# Copy the object's state from self.__dict__ which contains
		# all our instance attributes. Always use the dict.copy()
		# method to avoid modifying the original state.
		state = self.__dict__.copy()
		# Remove the unpicklable entries.
		del state['loggin']
		del state['manager']
		del state['change_list']
		return state

	def create_new_vid(self, name):
		m_v_main.clear_fetch()
		self.old_version_name = self.version_name
		self.version_name = name
		self.old_vid = self.vid
		self.vid = m_v_main.set(None, name).vid
		m_v_main.insert_set()
		m_v_main.clear_fetch()
		return

	def generate_change_list(self):
		#if self.old_vid == 0:
			#self.change_list = list(map(lambda x: f"A\t{x}" , git_file_list(self.version_name).splitlines()))
		#else:
		self.change_list = git_change_list(self.old_version_name, self.version_name)
		return self.change_list

	def processing_dirs(self):
		# Based on dirs
		command = [
			"find",
			f"{mf.version_dict[self.version_name]}",
			"-type",
			"d",
			"!",
			"-type",
			"l",
			"-printf",
			"%P\\n"
		]
		# the [1:] is for the blank line that this sh** command produce at the start
		dir_list = sp.run(command, capture_output=True, text=True).stdout.splitlines()[1:]

		if self.old_vid != 0:
			command = [
				"find",
				f"{mf.version_dict[self.old_version_name]}",
				"-type",
				"d",
				"!",
				"-type",
				"l",
				"-printf",
				"%P\\n"
			]

			old_dir_list = set(sp.run(command, capture_output=True, text=True).stdout.splitlines()[1:])
			dir_list = set(dir_list)
			new_dir_list = (dir_list - old_dir_list)


			# Unchanged dirs
			for single_dir in (dir_list - (dir_list - old_dir_list)):
				if m_file_name.get(m_file_name.fname(single_dir)) is None:
					new_dir_list.add(single_dir)
					print("Unchanged dirs: fname is None")
					print(single_dir)
					continue
				# Get old_bf
				old_bf = m_bridge_file.get(
					m_bridge_file.vid(gp.old_vid),
					m_bridge_file.fnid( m_file_name.get(m_file_name.fname(single_dir)).fnid )
				)
				m_bridge_file(gp.vid, old_bf.fnid, old_bf.fid)

			# New dirs
			for single_dir in new_dir_list:
				dir_file_name = m_file_name.get_set(m_file_name.fname(single_dir))
				dir_file = m_file(None, gp.vid, 0, 1, "A", 0)
				m_bridge_file(gp.vid, dir_file_name.fnid, dir_file.fid)
			# Deleted dirs
			for single_dir in (old_dir_list - dir_list):
				# Get old_bf
				if m_file_name.get(m_file_name.fname(single_dir)) is None:
					print("Deleted dirs: fname is None")
					print(single_dir)
					continue
				old_bf = m_bridge_file.get(
					m_bridge_file.vid(gp.old_vid),
					m_bridge_file.fnid( m_file_name.get(m_file_name.fname(single_dir)).fnid )
				)
				if old_bf is None:
					print("Deleted dirs: old_bf is None")
					print(single_dir)
					print(m_file_name.get(m_file_name.fname(single_dir)))
					continue

				# 0 Update old FILE
				m_file.update(
					old_bf.fid,
					None,
					gp.old_vid,
					None,
					None,
					"R"
				)

		else:
			# If VID = 1, we need all dirs to be added
			for single_dir in dir_list:
				dir_file_name = m_file_name.get_set(m_file_name.fname(single_dir))
				dir_file = m_file(None, gp.vid, 0, 1, "A", 0)
				m_bridge_file(gp.vid, dir_file_name.fnid, dir_file.fid)
		return

	def preload_fnid(self):
		# Based on change
		for changed_file in map(lambda x: x.split("\t")[-1], self.change_list):
			m_file_name.get_set(m_file_name.fname(changed_file))
		return

	def processing_changes(self):
		global multi_proc
		multi_proc = True
		self.manager = multiprocessing.Manager()
		self.shared_set_list = self.manager.list()
		processes = []

		try:
			for x in range(CPUS-1):
				if x == (CPUS-2):
					processes.append(multiprocessing.Process(target=file_processing, args=(len(gp.change_list)//int(CPUS-1)*x, None)))
				else:
					processes.append(multiprocessing.Process(target=file_processing, args=(len(gp.change_list)//int(CPUS-1)*x, len(gp.change_list)//int(CPUS-1)*(x+1))))
				processes[-1].start()

			for fp_instance in processes:
				fp_instance.join()
		except Exception as e:
			print("Error in gp.processing_changes()")
			print(e)
			emergency_shutdown(2)

		del processes
		multi_proc = False
		return

	def processing_unchanges(self):
		if self.old_vid == 0:
			return
		full_set = set(git_file_list(self.version_name).splitlines())
		changed_set = set(map(lambda x: x.split("\t")[-1], filter(lambda x: not x.startswith("D"), self.change_list)))
		unchanged_set = (full_set - changed_set)
		deleted_set = set(map(lambda x: x.split("\t")[-1], filter(lambda x: x.startswith("D"), self.change_list)))
		old_full_set = set(git_file_list(self.old_version_name).splitlines())
		forgotten_delete = ((old_full_set - full_set) - deleted_set)
		if forgotten_delete:
			print("There seems to be forgotten deletes... Processing...")
			if OVERRIDE_FORGOTTEN_PRINT:
				print(forgotten_delete)
			file_processing(0, 0, list(map(lambda x: f"D\t{x}" , forgotten_delete)))

		forgotten_new = ((full_set - old_full_set) - changed_set)
		if forgotten_new:
			print("There seems to be forgotten_new...")
			if OVERRIDE_FORGOTTEN_PRINT:
				print(forgotten_new)
			

		for unchanged in unchanged_set:
			old_bf = m_bridge_file.get(
				m_bridge_file.vid(gp.old_vid),
				m_bridge_file.fnid( m_file_name.get(m_file_name.fname(unchanged)).fnid )
			)
			if old_bf is None:
				print("processing_unchanges: old_bf is None")
				print(unchanged)
				print(m_file_name.get(m_file_name.fname(unchanged)))
				continue
			m_bridge_file(gp.vid, old_bf.fnid, old_bf.fid)
		return

	def push_set_to_main(self):
		self.shared_set_list.append(pickle.dumps(gp.main_dict))
		return

	def set(self, *args):
		self.append(args)
		return



	def execute(self):
		# doesn't include tags
		global multi_proc
		multi_proc = False
		for CS in self.main_dict.values():
			x = [] # ruff F841 : unused will we need it? if not, no soup for you!
			for item in CS.cs:
				while item.__class__.__name__ == "Delayed_Executor":
					item = item.process(CS.cs_result)
				CS.cs_result.append(item)

				if not CS.cs_result_dict.get(item.__class__.__name__):
					CS.cs_result_dict[item.__class__.__name__] = []
				CS.cs_result_dict[item.__class__.__name__].append(item)

			CS.cs_processed = True
			#print(f"execute() results:{x}")
		return

	def execute_all(self):
		print("Great_Processor.execute() start")
		if len(self.shared_set_list) != CPUS-1:
			print(f"You have {len(self.shared_set_list)} set_list. We need {CPUS-1}!!! Exiting now!")
			emergency_shutdown(3)
		for remote_gp in self.shared_set_list:
			self.main_dict.update(pickle.loads(remote_gp))
		self.execute()

		#self.handling_tags()
		self.main_dict = {}
		print("Great_Processor.execute_all() done")
		return

	def get_on_fname(self, fname, table, only_first=True):
		# Will return the table entry(ies) given a fname and table. Will look in currently processed CS and, if not found, search db for prior version
		#####NEEDS TO BE FIXED AS IT MAY RETURN MORE THAN EXPECTED AS WE DO GET_SET ON RANDOM THINGS..... (FOR THE MAIN_DICT PART...)
		if self.main_dict.get(fname):
			if (result := self.main_dict[fname].cs_result_dict.get(table)):
				if only_first:
					return result[0]
				else:
					return result

		if not (fn := m_file_name.get(m_file_name.fname(fname))):
			return None
		if table == "m_file_name":
			return fn


		if not (mbf := m_bridge_file.get(m_bridge_file.vid(self.old_vid), m_bridge_file.fnid(fn.fnid))):
			return None
		if table == "m_bridge_file":
			return mbf

		if table == "m_file":
			return m_file.get(m_file.fid(mbf.fid))

		## THIS ASSUMES THE ONLY THING WE COULD WANT AT THIS POINT IS INCLUDES
		if not (mbi := m_bridge_include.get(m_bridge_include.fid(mbf.fid))):
			return None
		if table == "m_bridge_include":
			return mbi

		if table == "m_include":
			return m_include.get(m_include.iid(mbi.iid))
		if table == "m_include_content":
			mic = m_include_content.get(m_include_content.iid(mbi.iid))
			if mic is None:
				return None
			if only_first:
				return mic[0]
			return mic

		# i don't know what you want!
		print(f"get_on_fname is not built for this! Returning None! Fname: {fname} Table: {table}")
		return None



	def handling_tags(self):

		while len(gp.main_dict) != 0:
			print("goat dead")





		return


	def insert_all(self):
		for table in self.loggin:
			table.insert_set()
			table.insert_update()
		return

	def drop_all(self):
		sql_drop = "DROP TABLE "
		db = set_db()
		execdb(db, "SET FOREIGN_KEY_CHECKS = 0;")
		sql_drop_print = "DROP TABLE "
		for table in self.loggin:
			if len(sql_drop_print.splitlines()[-1]) > OVERRIDE_MAX_PRINT_SIZE:
				sql_drop_print += "\n"
			sql_drop_print += f"`{table.table_name}`, "
			sql_drop += f"`{table.table_name}`, "
		print(sql_drop_print[:-2])
		#for _ in range(3): # ruff F841 we don't use the variable, so we dont need one.
		if True:
			try:
				execdb(db, sql_drop[:-2])
			except Exception as e: 
				print(f"drop failed : exception : {e}") 
		unset_db(db)
		return


	def clear_fetch_all(self):
		for table in self.loggin:
			table.clear_fetch()
		return

	def print_all_set(self):
		for table in self.loggin:
			print(table.set_table)
		return

gp = Great_Processor()

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
X = Referenced_Element()

class Delayed_Executor:
	"""
	Preserves the values in set/update command, works in tandem with GP.
	"""
	def __init__(self, table, action, item):
		self.table = table
		self.action = action
		self.item = item

	def process(self, array):
		return getattr(globals()[self.table], self.action)(self.item, array)


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
		# where x = gp
		self.select_procedure = select_procedure
		
		gp.loggin.append(self)
		return


	class Column_Class:
		def __init__(self, table_name: str, column_id: int):
			self.table_name = table_name
			self.column_id = column_id
			return

		def __call__(self, value=None):
			return (self.table_name, self.column_id, value)

	def create_table(self):
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


	def clear_fetch(self):
		self.optimized_table = {}
		self.optimized_table_list = {}
		self.current_table = {}
		self.set_table = {}
		self.update_table = {}

		if self.select_procedure is False:
			return

		if isinstance(self.select_procedure, types.LambdaType):
			select_command = self.select_procedure(gp)
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

	def set(self, *item):
		if multi_proc:
			return Delayed_Executor(self.table_name, "dset", item)

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

	def dset(self, items, array):
		output = []
		for item in items:
			if item.__class__.__name__ == "Referenced_Element":
				output.append(item.get_value(array))
			else:
				output.append(item)
		return self.set(*output)

	def get_set(self, *columns):
		temp = self.get(*columns)

		if temp is not None:
			return temp

		temp_arr = [None] * len(self.init_columns)
		for item in columns:
			temp_arr[item[1]] = item[2]
		return self.set(*temp_arr)

	# CAN RETURN NONE IF NO ITEM AT PRIMARY KEY
	def update(self, *item):
		if multi_proc:
			return Delayed_Executor(self.table_name, "dupdate", item)

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

	def dupdate(self, items, array):
		output = []
		for item in items:
			if item.__class__.__name__ == "Referenced_Element":
				output.append(item.get_value(array))
			else:
				output.append(item)
		return self.update(*output)

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


class Master_File:
	def __init__(self):
		self.version_dict = {}
		self.file_dict = {}


	def add_version(self, version_name=None):
		if version_name is None:
			version_name=gp.version_name

		self.version_dict[version_name] = git_clone(version_name)
		PURGE_LIST.append(self.version_dict[version_name])
		self.file_dict[version_name] = {}

	def trim_version(self, keep=2):
		if len(self.version_dict) > keep:
			print("Removing old version_dict")
			shutil.rmtree(self.version_dict[next(iter(self.version_dict))])
			del self.version_dict[next(iter(self.version_dict))]
			del self.file_dict[next(iter(self.file_dict))]
			return 1
		return 0

	def clear_all_version(self):
		for item in self.version_dict:
			shutil.rmtree(self.version_dict[item])
			# Maple's weirdest friend, Ned the Fox
		return

	def get_file(self, file_path, version=None):
		if version is None:
			version=gp.version_name
		if version not in self.version_dict:
			command = [
				"git",
				"--git-dir=linux/.git",
				"show",
				f"{version}:{file_path}"
			]
			raw_file = sp.run(command, capture_output=True, text=True, encoding='latin-1')
			return raw_file.stdout
		else:
			if file_path not in self.file_dict[version]:
				self.file_dict[version][file_path] = Path(f"{self.version_dict[version]}/{file_path}").read_text(encoding='latin-1')
		return self.file_dict[version][file_path]


	def get_includes(self, file_path, version=None):
		temp_type = type_check(file_path)
		if not (temp_type == 2 or temp_type == 3):
			return False

		if version is None:
			version=gp.version_name

		try:
			if (current_file := self.get_file(file_path, version)) is f"fatal: path '{file_path}' does not exist in '{version}'":
				return False
		except FileNotFoundError:
			return False

		results = tuple(filter(lambda x: x.startswith("#include"), current_file.splitlines()))
		if results:
			temp_arr = []
			final_arr = []
			for item in results:
				try:
				#include 			 <my_aids.h­­>
					match item[9]:
						case "<":
							temp_arr.append("include/" + item[10:item.find(">")])
						case '"':
							temp_arr.append(f"{Path(file_path).parent}/" + item[10:(item[10:].find('"')+10)])
						case _:
							if not CLEAN_PRINT:
								print(f"Unrecognised include: {item}")
				except Exception:
					print(f"Unrecognised include causing an error: {item}")
			for item in filter(str.strip, temp_arr):
				path_arr = []
				dotdot = 0
				for chunk in item.split("/")[::-1]:
					if chunk == "..":
						dotdot += 1
					elif dotdot > 0:
						dotdot -= 1
					else:
						path_arr.append(chunk)
				final_arr.append("/".join(path_arr[::-1]))
			if final_arr:
				return final_arr
		return False
mf = Master_File()




########### GIT UTILS ###########
def git_change_list(old_vn, vn):
	command = [
		"git",
		"--git-dir=linux/.git",
		"diff",
		f"{old_vn}",
		f"{vn}",
		"--name-status"
	]

	raw_file_list = sp.run(command, capture_output=True, text=True)
	return raw_file_list.stdout.splitlines()

def git_clone(version):
	temp_path = create_temp_dir()
	command = [
		"git",
		"clone",
		f"{linux_directory}",
		"--branch",
		f"{version}",
		f"{temp_path}",
		"-c advice.detachedHead=false"
	]
	
	sp.run(command)
	shutil.rmtree(f"{temp_path}/.git")
	command = [
		"ln",
		"-s",
		"asm-generic",
		f"{temp_path}/include/asm"
	]
	sp.run(command)
	command = [
		"ln",
		"-s",
		"asm-generic",
		f"{temp_path}/include/uapi/asm"
	]
	sp.run(command)
	return temp_path

def git_file_list(version):
	command = [
		"git",
		"--git-dir=linux/.git",
		"ls-tree",
		"-r",
		"--name-only",
		f"{version}"
	]
	raw_file = sp.run(command, capture_output=True, text=True)
	return raw_file.stdout



########### GIT UTILS ###########

def create_temp_dir():
	command = [
		"mktemp",
		"-d",
		"-p",
		f"{RAMDISK}",
		"kernel-parser.XXXXXX"
	]
	output = sp.run(command, capture_output=True, text=True)
	return output.stdout.strip()

def type_check(name):
	#dirs are for 1
	if name.endswith((".c")):
		return 2
	elif name.endswith((".h")):
		return 3
	elif name.endswith(("Kconfig")):
		return 4
	else:
		return 0


def file_processing(start, end=None, override_list=None):
	global multi_proc
	if override_list:
		changed_files = override_list
		multi_proc = True
	else:
		if end is None:
			changed_files = gp.change_list[start:]
		else:
			changed_files = gp.change_list[start:end]
		#print(changed_files, flush=True)
	for changed_file in changed_files:
		cut_file = tuple(changed_file.split("\t"))
		CS = None
		try:
			match cut_file[0][0]:
				case "D":
					# DELETE
					CS = Change_Set(cut_file[1])
					# Get old_bf
					old_bf = m_bridge_file.get(
						m_bridge_file.vid(gp.old_vid),
						m_bridge_file.fnid( m_file_name.get( m_file_name.fname(cut_file[1]) ).fnid )
					)
					if old_bf is None:
						print("case \"D\": old_bf is None")
						print(cut_file[1])
						print(m_file_name.get(m_file_name.fname(cut_file[1])))
						continue

					# 0 Update FILE
					CS(m_file.update(
						old_bf.fid,
						None,
						gp.old_vid,
						None,
						None,
						"D"
					))

				case "R":
					CS = Change_Set(cut_file[2])

					if cut_file[0][1:4] == "100":
						# Exact Moved
						# Get old_bf
						old_bf = m_bridge_file.get(
							m_bridge_file.vid(gp.old_vid),
							m_bridge_file.fnid( m_file_name.get( m_file_name.fname(cut_file[1]) ).fnid )
						)
						if old_bf is None:
							print("case \"R\": if 100: old_bf is None")
							print(cut_file[1])
							print(m_file_name.get(m_file_name.fname(cut_file[1])))
							raise _MyBreak
						# 0 Update old FILE
						CS(m_file.update(
							old_bf.fid,
							None,
							gp.old_vid,
							None,
							None,
							"R"
						))

						# 1 Check if FNAME exist/Create FNAME
						CS(m_file_name.get_set(m_file_name.fname(cut_file[2])))
						# 2 Create FILE
						CS(m_file(None, gp.vid, 0, type_check(cut_file[2]), "R", 0))
						# 3 Create BRIDGE FILE
						CS(m_bridge_file(gp.vid, X[1].fnid, X[2].fid))

						# 4 Create MOVED FILE
						CS(m_moved_file(old_bf.fid, X[2].fid))

					else:
						# RENAME MODIFY
						# Get old_bf
						old_bf = m_bridge_file.get(
							m_bridge_file.vid(gp.old_vid),
							m_bridge_file.fnid( m_file_name.get( m_file_name.fname(cut_file[1]) ).fnid )
						)
						if old_bf is None:
							print("case \"R\": else: old_bf is None")
							print(cut_file[1])
							print(m_file_name.get(m_file_name.fname(cut_file[1])))
							raise _MyBreak

						# 0 Update old FILE
						CS(m_file.update(
							old_bf.fid,
							None,
							gp.old_vid,
							None,
							None,
							"R"
						))

						# 1 Check if FNAME exist/Create FNAME
						CS(m_file_name.get_set(m_file_name.fname(cut_file[2])))
						# 2 Create FILE
						CS(m_file(None, gp.vid, 0, type_check(cut_file[2]), "R", 0))
						# 3 Create BRIDGE FILE
						CS(m_bridge_file(gp.vid, X[1].fnid, X[2].fid))

						# 4 Create MOVED FILE
						CS(m_moved_file(old_bf.fid, X[2].fid))

				case "M":
					# MODIFY
					CS = Change_Set(cut_file[1])
					# Get old_bf
					old_bf = m_bridge_file.get(
						m_bridge_file.vid(gp.old_vid),
						m_bridge_file.fnid( m_file_name.get( m_file_name.fname(cut_file[1]) ).fnid )
					)
					if old_bf is None:
						print("case \"M\": old_bf is None")
						print(cut_file[1])
						print(m_file_name.get(m_file_name.fname(cut_file[1])))
						#print(m_bridge_file.current_table)
						raise _MyBreak

					# 0 Update old FILE
					CS(m_file.update(
						old_bf.fid,
						None,
						gp.old_vid,
						None,
						None,
						"M"
					))

					# 1 Create FILE
					CS(m_file(None, gp.vid, 0, type_check(cut_file[1]), "M", 0))
					# 2 Create BRIDGE FILE
					CS(m_bridge_file(gp.vid, old_bf.fnid, X[1].fid))

		except _MyBreak:
			if CS:
				print(f"This failed bad after a _MyBreak... : {CS}")
				continue

		if not CS:
			# Add or other
			CS = Change_Set(cut_file[1])
			# 0 Check if FNAME exist/Create FNAME
			CS(m_file_name.get_set(m_file_name.fname(cut_file[1])))

			# 1 Create FILE
			CS(m_file(None, gp.vid, 0, type_check(cut_file[1]), "A", 0))
			# 2 Create BRIDGE FILE
			CS(m_bridge_file(gp.vid, X[0].fnid, X[1].fid))


		# Store Set
		gp.main_dict[CS.file_name] = CS

	if override_list:
		multi_proc = False
	else:
		gp.push_set_to_main()
	return


def update(version):
	print(green(f"=======================Working on {version}======================="))
	gp.create_new_vid(version)
	gp.clear_fetch_all()
	# Pre-Processing
	mf.add_version()

	gp.generate_change_list()

	## preload/dirs
	m_file_name.gen_optimized_table(m_file_name.fname())
	gp.processing_dirs()
	gp.preload_fnid()
	m_file_name.insert_set()
	m_file_name.clear_fetch()
	m_file.insert_set()
	m_file.clear_fetch()
	m_bridge_file.insert_set()
	m_bridge_file.clear_fetch()
	## preload/dirs End
	# Optimization
	m_file_name.gen_optimized_table(m_file_name.fname())

	# Main Processing
	gp.processing_changes()
	gp.processing_unchanges()

	gp.execute_all()
	#gp.print_all_set()
	gp.insert_all()
	return


def main():
	# demo String_shortner

	#def random_line(min_chars=500, max_chars=2000):
	#	import random, string
	#	words = []
	#	total_len = 0

	#	while total_len < min_chars :
	#		word = "".join(random.choices(string.ascii_lowercase, k=random.randint(10, 30))) # k=length
	#		words.append(word)
	#		total_len += len(word) + 1  # + space

	#	return " ".join(words)[:max_chars]

	#demo_data = random_line()
	#print(demo_data)
	#print(String_shortner(demo_data)) # default is mode="boxed+indent". other mode are : "boxed", "indent".
	# end of demo String_shortner

	arg_handling()
	gp.drop_all()
	initialize_db()
	update("v3.0") 
	update("v3.1") 
	update("v3.2") 
	update("v3.3") 
	update("v3.4") 
	update("v3.5") 
	emergency_shutdown(0)
	return

def arg_handling():
	parser = argparse.ArgumentParser()
	parser.add_argument("-D", "--Drop", help="Drop all tables", action='store_true')
	parser.add_argument("-C", "--Create-Tables", help="Generate all tables", action='store_true')
	parser.add_argument("-T", "--Test", help="Test/Parse a specific file")
	args = parser.parse_args()

	if args.Drop:
		print("Dropping all tables")
		gp.drop_all()
		emergency_shutdown(0)
	if args.Create_Tables:
		initialize_db()
		emergency_shutdown(0)
	if args.Test:
		gp.drop_all()
		initialize_db()
		gp.clear_fetch_all()
		gp.create_new_vid("v3.0")
		mf.add_version()
		#include/linux/netfilter_bridge/ebtables.h
		#include/linux/lockd/bind.h
		#include/linux/sched.h
		Ast_Manager(mf.version_dict[gp.version_name], args.Test)
		emergency_shutdown(0)
	return

##################################
# DB STRUCTURE

m_v_main = Table(
	"m_v_main",
	(
		("vid", "INT", "NOT NULL", "AUTO_INCREMENT"),
		("vname", "VARCHAR(32)", "NOT NULL", "COLLATE utf8mb4_bin")
	),
	("vid",),
	None,
	((0,"latest"),),
	True,
	True
)

m_file_name = Table(
	"m_file_name",
	(
		("fnid", "INT", "NOT NULL", "AUTO_INCREMENT"),
		("fname", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin")
	),
	("fnid",),
	None,
	((0,""),),
	True,
	True
)

m_file = Table("m_file",
	(
		("fid", "INT", "NOT NULL", "AUTO_INCREMENT"),
		("vid_s", "INT", "NOT NULL"),
		("vid_e", "INT", "NOT NULL"),
		("ftype", "TINYINT", "UNSIGNED", "NOT NULL"),
		("s_stat", "CHAR(1)", "NOT NULL"),
		("e_stat", "CHAR(1)", "NOT NULL")
	),
	("fid",),
	(
		("vid_s", "m_v_main", "vid"),
		("vid_e", "m_v_main", "vid")
	),
	((0,0,0,0,0,0)),
	False,
	lambda x: f"SELECT m_file.* FROM m_file INNER JOIN m_bridge_file ON m_bridge_file.fid = m_file.fid WHERE m_bridge_file.vid = {x.old_vid};"
)

m_bridge_file = Table(
	"m_bridge_file",
	(
		("vid", "INT", "NOT NULL"),
		("fnid", "INT", "NOT NULL"),
		("fid", "INT", "NOT NULL")
	),
	("vid","fnid"),
	(
		("vid", "m_v_main", "vid"),
		("fnid","m_file_name","fnid"),
		("fid","m_file","fid")
	),
	None,
	False,
	lambda x: f"SELECT * FROM m_bridge_file WHERE m_bridge_file.vid = {x.old_vid};"
)

m_moved_file = Table(
	"m_moved_file",
	(
		("s_fid", "INT", "NOT NULL"),
		("e_fid", "INT", "NOT NULL")
	),
	("s_fid","e_fid"),
	(
		("s_fid", "m_file", "fid"),
		("e_fid", "m_file", "fid")
	),
	None,
	False,
	False
)

m_ast = Table(
	"m_ast",
	(
		("ast_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
		("name", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
		("type_id", "TINYINT", "UNSIGNED", "NOT NULL")	
	),
	("ast_id",),
	None,
	(0,"",0),
	False,
	True
)

m_ast_container = Table(
	"m_ast_container",
	(
		("ast_id", "INT", "NOT NULL"),
		("priority", "TINYINT", "UNSIGNED", "NOT NULL"),
		("name", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
		("subtype", "TINYINT", "UNSIGNED", "NOT NULL"),
		("ref_ast_id","INT", "NOT NULL")
	),
	("ast_id","priority"),
	(
		("ast_id","m_ast","ast_id"),
		("ref_ast_id","m_ast","ast_id")
	),
	None,
	False,
	True
)

m_ast_include = Table(
	"m_ast_include",
	(
		("ast_id", "INT", "NOT NULL"),
		("fnid", "INT", "NOT NULL")
	),
	("ast_id",),
	(
		("ast_id","m_ast","ast_id"),
		("fnid","m_file_name","fnid")
	),
	None,
	True,
	True
)

m_ast_debug = Table(
	"m_ast_debug",
	(
		("ast_id", "INT", "NOT NULL"),
		("ast_raw", "JSON", "NOT NULL")
	),
	("ast_id",),
	(("ast_id","m_ast","ast_id"),),
	None,
	False,
	False
)

m_tag = Table(
	"m_tag",
	(
		("tag_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
		("vid_s", "INT", "NOT NULL"),
		("vid_e", "INT", "NOT NULL"),
		("code", "LONGTEXT", "NOT NULL"),
		("ast_id", "INT", "NOT NULL"), #need an index
		("hl_s", "INT", "NOT NULL"),
		("hl_l", "INT", "NOT NULL")
	),
	("tag_id","vid_s"),
	(("vid_s", "m_v_main", "vid"),("vid_e", "m_v_main", "vid"),("ast_id","m_ast","ast_id")),
	(0,0,0,"",0,0,0),
	False,
	True #will need to update to only get the last version of the tags
)

m_bridge_tag = Table(
	"m_bridge_tag",
	(
		("fid", "INT", "NOT NULL"),
		("tag_id", "INT", "NOT NULL"),
		("line_s", "INT", "NOT NULL"),
		("line_e", "INT", "NOT NULL")
	),
	("fid","tag_id"),
	(
		("fid", "m_file", "fid"),
		("tag_id", "m_tag", "tag_id")
	),
	None,
	False,
	True #will need to update to only get the last version of the tags
)

# DB STRUCTURE END
##################################

def initialize_db():
	if OVERRIDE_TABLE_CREATION_PRINT:
		print("Creating m_v_main")
	m_v_main.create_table()

	if OVERRIDE_TABLE_CREATION_PRINT:
		print("Creating m_file_name")
	m_file_name.create_table()

	if OVERRIDE_TABLE_CREATION_PRINT:
		print("Creating m_file")
	m_file.create_table()

	if OVERRIDE_TABLE_CREATION_PRINT:
		print("Creating m_bridge_file")
	m_bridge_file.create_table()

	if OVERRIDE_TABLE_CREATION_PRINT:
		print("Creating m_moved_file")
	m_moved_file.create_table()

	if OVERRIDE_TABLE_CREATION_PRINT:
		print("Creating m_ast")
	m_ast.create_table()

	if OVERRIDE_TABLE_CREATION_PRINT:
		print("Creating m_ast_container")
	m_ast_container.create_table()

	if OVERRIDE_TABLE_CREATION_PRINT:
		print("Creating m_ast_include")
	m_ast_include.create_table()

	if OVERRIDE_TABLE_CREATION_PRINT:
		print("Creating m_ast_debug")
	m_ast_debug.create_table()

	if OVERRIDE_TABLE_CREATION_PRINT:
		print("Creating m_tag")
	m_tag.create_table()

	if OVERRIDE_TABLE_CREATION_PRINT:
		print("Creating m_bridge_tag")
	m_bridge_tag.create_table()

	return

if __name__ == "__main__":
	main()
