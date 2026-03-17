from globalstuff import *
from operator import itemgetter


# Table Engine
class TE_direct_db():
	def __init__(self):
		self.tables = {}
		self.queued_set = {}
		self.queued_update = {}
		self.queued_view = {}
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

		self.queued_view = {}

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


	def view_get(self, joins, columns):
		return self.db.view_select(self.tables, joins, columns)

	def view_set(self, joins, columns):
		filtered_columns = tuple(filter(lambda val: val is not None, columns))

		try:
			current_view = self.queued_view[joins].get(filtered_columns)
			return tuple(map(lambda val: val if val is not None else current_view, columns))
		except KeyError:
			current_view = None
			
		if self.queued_view.get(joins) is None:
			self.queued_view[joins] = {}

		self.queued_view[joins][filtered_columns] = self.next_id[joins[0][0][0]]
		self.next_id[joins[0][0][0]] += 1

		result = tuple(map(lambda val: val if val is not None else self.next_id[joins[0][0][0]]-1, columns))
		
		self.queued_set[joins[0][0][0]][result[:1]] = result[:len(self.tables[joins[0][0][0]].init_columns)]
		if len(joins[0]) == 1:
			return result
		mod_result = tuple(result[len(self.tables[joins[0][0][0]].init_columns):])

		for i, join in enumerate(joins):
			for x in range(join[2]):
				self.queued_set[join[1][0]][mod_result[:1]] = mod_result[:len(self.tables[join[1][0]].init_columns)]
				mod_result = tuple(mod_result[len(self.tables[join[1][0]].init_columns):])

		return result

	def update(self, table_id, columns):
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

