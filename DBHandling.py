import os
import mysql.connector
import json

MAX_ALLOWED_PACKET = 1073741824

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
	execdb(db, f"SET GLOBAL max_allowed_packet = {MAX_ALLOWED_PACKET};")
	# 10GB max size
	return db

def unset_db(db):
	db[1].close()
	db[0].close()
	return []

def execdb(db, sql, data=None):
	if data:
		if type(data[0]) is tuple:
			#length = len(json.dumps(data))
		
			if (MAX_ALLOWED_PACKET//512) < len(data):
				temp_len = len(data)
				x = 0
				while x < temp_len:
					if (x+(MAX_ALLOWED_PACKET//512)) > temp_len:
						print(f"x+(MAX_ALLOWED_PACKET/512)={x+(MAX_ALLOWED_PACKET//512)}")
						print(f"{x}")
						db[1].executemany(sql, data[x:])
					else:
						print("else")
						print(f"x+(MAX_ALLOWED_PACKET/512)={x+(MAX_ALLOWED_PACKET//512)}")
						print(f"{x}")
						db[1].executemany(sql, data[x:(x+(MAX_ALLOWED_PACKET//512))])
					x += (MAX_ALLOWED_PACKET//512)
				return db[0].commit()
			else:		
				db[1].executemany(sql, data)
				return db[0].commit()
	db[1].execute(sql, data)
	return db[0].commit()

def selectdb(db, sql):
	db[1].execute(sql)
	return
########### DB UTILS ###########