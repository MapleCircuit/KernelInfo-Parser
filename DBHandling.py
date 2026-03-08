from globalstuff import *
import os
import mysql.connector
import json



MAX_ALLOWED_PACKET = 1073741824

class mysql_db():
	def __init__(self):
		self.user = "root"
		self.password = "Passe123"
		self.db_name = "test"
		self.cnx = self.connect_sql()
		self.cursor = self.cnx.cursor()
		self.cursor.execute("SET SESSION sql_mode = 'NO_AUTO_VALUE_ON_ZERO';")
		self.cursor.execute(f"SET GLOBAL max_allowed_packet = {MAX_ALLOWED_PACKET};")
		self.cnx.commit()

	def __del__(self):
		self.cursor.close()
		self.cnx.close()
		return

	def connect_sql(self):
		if os.path.exists('/.dockerenv') or 'docker' in open('/proc/self/cgroup').read():
			mysql_host = "host.docker.internal"
		else :
			mysql_host = "localhost"
		# Connect to DB
		return mysql.connector.connect(host=mysql_host, user=self.user, password=self.password, database=self.db_name)

	def drop_table(self, tables):
		if not isinstance(tables, (tuple, list)):
			tables = (tables,)

		self.check_if_connected()
		self.cursor.execute("SET FOREIGN_KEY_CHECKS = 0;")
		self.cnx.commit()

		sql_drop = "DROP TABLE "
		sql_drop += ", ".join(map(lambda x:f"`{x.table_name}`", tables))

		try:
			self.cursor.execute(sql_drop)
			self.cnx.commit()
		except Exception as e: 
			print(f"drop failed : exception : {e}") 

		return

	def create_table(self, tables):
		if not isinstance(tables, (tuple, list)):
			tables = (tables,)

		self.check_if_connected()

		for table in tables:
			sql_table = f"CREATE TABLE {table.table_name} "
			sql_table += f"({", ".join(map(" ".join, table.init_columns))} "
			sql_table += f", PRIMARY KEY ({", ".join(table.init_primary)}) "
			if table.init_foreign:
				sql_table += " ".join(map(lambda x: f",FOREIGN KEY ({x[0]}) REFERENCES {x[1]}({x[2]})", table.init_foreign))
			sql_table += ")"
			if OVERRIDE_TABLE_CREATION_PRINT:
				print(f"Created table:{table.table_name}")
				print(sql_table)
			self.check_if_connected()
			self.cursor.execute(sql_table)
			if table.initial_insert:
				self.insert(table, table.initial_insert)


		self.cnx.commit()
		return

	def get_next_id(self, table):
		self.check_if_connected()

		self.cursor.execute(f"SELECT COALESCE(MAX({table.init_columns[0][0]}), 0)+1 FROM {table.table_name};")
		
		return self.cursor.fetchone()[0]

	def check_if_connected(self):
		for attempt in range(3):
			if self.cnx.is_connected:
				break
			print(f"No SQL connection attempt:{attempt}")
			self.cnx = self.connect_sql()
			self.cursor = self.cnx.cursor()
			self.cursor.execute("SET SESSION sql_mode = 'NO_AUTO_VALUE_ON_ZERO';")
			self.cursor.execute(f"SET GLOBAL max_allowed_packet = {MAX_ALLOWED_PACKET};")
			self.cnx.commit()

	def insert(self, table, data):
		self.check_if_connected()

		sql = f'INSERT INTO {table.table_name} VALUES ({",".join(("%s",) * len(table.init_columns))})'

		if isinstance(data[0], (tuple, list)):	
			if (MAX_ALLOWED_PACKET//512) < len(data):
				temp_len = len(data)
				x = 0
				while x < temp_len:
					if (x+(MAX_ALLOWED_PACKET//512)) > temp_len:
						self.cursor.executemany(sql, data[x:])
					else:
						self.cursor.executemany(sql, data[x:(x+(MAX_ALLOWED_PACKET//512))])
					x += (MAX_ALLOWED_PACKET//512)
				self.cnx.commit()
				return 
			else:		
				self.cursor.executemany(sql, data)
				self.cnx.commit()
				return
		else:
			self.cursor.execute(sql, data)
			self.cnx.commit()
		return
	

	def update(self, table, data):
		self.check_if_connected()

		sql = f'INSERT INTO {table.table_name} ({", ".join(map(lambda column: column[0], table.init_columns))}) VALUES ({",".join(("%s",) * len(table.init_columns))}) ON DUPLICATE KEY UPDATE '
		updatable_columns = []
		for x, column in enumerate(table.init_columns):
			if x not in table.primary:
				updatable_columns.append(f"{column[0]} = VALUES({column[0]})")
		sql += ", ".join(updatable_columns)

		self.cursor.executemany(sql, data)
		self.cnx.commit()
		return

	def select(self, table, data):
		self.check_if_connected()

		sql = f"SELECT * FROM {table.table_name} WHERE "

		temp = []
		temp2 = []
		for x, val in enumerate(data):
			if val:
				temp.append(f"{table.init_columns[x][0]}=%s")
				temp2.append(val)

		#temp2 = tuple(temp2)

		sql +=  " AND ".join(temp)
		self.cursor.execute(sql, temp2)

		return self.cursor.fetchone()
