from globalstuff import *
import multiprocessing
import pickle


class Great_Processor:
	def __init__(self):
		self.PURGE_LIST = []
		self.Table_Array = []
		self.Version_Name = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
		self.Old_Version_Name = 0
		self.VID = 0
		self.Old_VID = 0
		self.Change_List = None
		self.Change_Set_Dict = {}
		self.Manager = None
		self.Shared_Change_Set_Dict_List = None
		
	def push_set_to_main(self):
		self.Shared_Change_Set_Dict_List.append(pickle.dumps(self.Change_Set_Dict))
		return

	def start_manager(self):
		self.Manager = multiprocessing.Manager()
		self.Shared_Change_Set_Dict_List = self.Manager.list()
		return

	def stop_manager(self):
		for Shared_Change_Set_Dict in self.Shared_Change_Set_Dict_List:
			self.Change_Set_Dict.update(pickle.loads(Shared_Change_Set_Dict))

		del self.Manager
		del self.Shared_Change_Set_Dict_List
		self.Manager = None
		self.Shared_Change_Set_Dict_List = []
		return

	def reset_cs(self):
		del self.Change_List
		del self.Change_Set_Dict
		self.Change_List = None
		self.Change_Set_Dict = {}
		return