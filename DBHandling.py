import os
import mysql.connector

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