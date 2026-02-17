from globalstuff import *
from DBHandling import *
import subprocess as sp
import multiprocessing
import pickle


class Great_Processor:
	def __init__(self):
		self.manager = None
		self.shared_set_list = None
		self.main_dict = {}
		self.file_processing = None # NEEDS TO BE SET
		self.Version_Name = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
		self.Old_Version_Name = 0
		self.VID = 0
		self.Old_VID = 0
		self.PURGE_LIST = []
		self.multi_proc = False
		self.Table_array = []

	def __getstate__(self):
		#useless
		# Copy the object's state from self.__dict__ which contains
		# all our instance attributes. Always use the dict.copy()
		# method to avoid modifying the original state.
		state = self.__dict__.copy()
		# Remove the unpicklable entries.
		del state['manager']
		del state['change_list']

		return state

	def create_new_vid(self, name):
		self.Table_array[0].clear_fetch(self)
		
		self.Old_Version_Name = self.Version_Name
		self.Version_Name = name
		self.Old_VID = self.VID
		self.VID = self.Table_array[0].actual_set(None, name).vid
		self.Table_array[0].insert_set()
		self.Table_array[0].clear_fetch(self)
		return

	def generate_change_list(self):
		command = [
			"git",
			"--git-dir=linux/.git",
			"diff",
			f"{self.Old_Version_Name}",
			f"{self.Version_Name}",
			"--name-status"
		]

		self.change_list = sp.run(command, capture_output=True, text=True).stdout.splitlines()
		return self.change_list

	def git_file_list(self, version):
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



	def processing_changes(self, func):
		self.multi_proc = True
		self.manager = multiprocessing.Manager()
		self.shared_set_list = self.manager.list()
		processes = []

		try:
			for x in range(CPUS-1):
				if x == (CPUS-2):
					processes.append(multiprocessing.Process(target=func, args=(len(self.change_list)//int(CPUS-1)*x, None)))
				else:
					processes.append(multiprocessing.Process(target=func, args=(len(self.change_list)//int(CPUS-1)*x, len(self.change_list)//int(CPUS-1)*(x+1))))
				processes[-1].start()

			for fp_instance in processes:
				fp_instance.join()
		except Exception as e:
			print("Error in gp.processing_changes()")
			print(e)
			emergency_shutdown(2)

		del processes
		self.multi_proc = False
		return

	def push_set_to_main(self):
		self.shared_set_list.append(pickle.dumps(self.main_dict))
		return

	def set(self, *args):
		self.append(args)
		return


	def execute(self):
		self.multi_proc = False
		for CS in self.main_dict.values():
			for item in CS.cs:
				while item.__class__.__name__ == "Delayed_Executor":
					item = item.process(CS.cs_result)
				CS.cs_result.append(item)

				if not CS.cs_result_dict.get(item.__class__.__name__):
					CS.cs_result_dict[item.__class__.__name__] = []
				CS.cs_result_dict[item.__class__.__name__].append(item)

			CS.cs_processed = True
			#print(f"execute() results:{CS.cs_result}")
		return

	def execute_all(self):
		print("Great_Processor.execute() start")
		if len(self.shared_set_list) != CPUS-1:
			print(f"You have {len(self.shared_set_list)} set_list. We need {CPUS-1}!!! Exiting now!")
			emergency_shutdown(3)
		for remote_gp in self.shared_set_list:
			self.main_dict.update(pickle.loads(remote_gp))
		self.execute()

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


	def insert_all(self):
		for table in self.Table_array:
			table.insert_set()
			table.insert_update()
		return

	def drop_all(self):
		sql_drop = "DROP TABLE "
		db = set_db()
		execdb(db, "SET FOREIGN_KEY_CHECKS = 0;")
		sql_drop_print = "DROP TABLE "
		for table in self.Table_array:
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

	def create_table_all(self):
		for table in self.Table_array:
			table.create_table()
		return

	def clear_fetch_all(self):
		for table in self.Table_array:
			table.clear_fetch(self)
		return

	def print_all_set(self):
		for table in self.Table_array:
			print(f"{table.table_name}={table.set_table}")
		return