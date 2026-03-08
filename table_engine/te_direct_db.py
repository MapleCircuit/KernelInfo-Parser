from globalstuff import *
from operator import itemgetter


# Table Engine
class TE_direct_db():
	def __init__(self):
		self.tables = {}
		self.queued_set = {}
		self.queued_update = {}
		self.next_id = {}
		self.db = None
		pass

	def __del__(self):
		del self.queued_set
		del self.queued_update
		del self.db
		return

	def start_new_db(self, DB):
		del self.db
		self.db = DB()
		return

	def start(self, tables, DB):
		if not isinstance(tables, (tuple, list)):
			tables = (tables,)

		self.db = DB()

		for table in tables:
			self.tables[table.gpid] = table
			self.queued_set[table.gpid] = {}
			self.queued_update[table.gpid] = []
			self.next_id[table.gpid] = self.db.get_next_id(table)

		return

	def get(self, table_id, columns):
		return self.db.select(self.tables[table_id], columns)

	def set(self, table_id, columns):
		if self.tables[table_id].no_duplicate:
			
			current_set = self.queued_set[table_id].get(columns[1:])
			if current_set:
				return (current_set, *columns[1:])

			self.queued_set[table_id][columns[1:]] = self.next_id[table_id]
			self.next_id[table_id] += 1

			return (self.next_id[table_id]-1, *columns[1:])
			

		if columns[0] == None:
			self.queued_set[table_id][self.next_id[table_id]] = (self.next_id[table_id], *columns[1:])
			self.next_id[table_id] += 1
			return self.queued_set[table_id][self.next_id[table_id]-1]

		self.queued_set[table_id][itemgetter(*self.tables[table_id].primary)(columns)] = columns
		
		return columns

	def update(self, table_id, columns):
		primary_values = itemgetter(*self.tables[table_id].primary)(columns)
		primary_values = primary_values if type(primary_values) is tuple else (primary_values,)
		get_columns = primary_values + (None,)*(len(columns)-len(self.tables[table_id].primary))

		get_result = self.get(table_id, get_columns)

		columns = tuple(map(lambda x: x[1] if x[1] is not None else get_result[x[0]], enumerate(columns)))
		self.queued_update[table_id].append(columns)
		return columns

	def commit(self, table_id):

		if self.queued_set[table_id]:
			if self.tables[table_id].no_duplicate:
				self.db.insert(
					self.tables[table_id],
					tuple(map(lambda x: (x[1], *x[0]), self.queued_set[table_id].items()))
				)
			else:
				self.db.insert(
					self.tables[table_id],
					tuple(self.queued_set[table_id].values())
				)

		if self.queued_update[table_id]:
			self.db.update(
				self.tables[table_id],
				tuple(self.queued_update[table_id])
			)
		return
