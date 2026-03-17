from fastapi import FastAPI
import mysql.connector
import os

app = FastAPI()


class mysql_db():
	def __init__(self):
		self.user = "root"
		self.password = "Passe123"
		self.db_name = "test"
		self.cnx = self.connect_sql()
		self.cursor = self.cnx.cursor()

	def connect_sql(self):
		if os.path.exists('/.dockerenv') or 'docker' in open('/proc/self/cgroup').read():
			mysql_host = "host.docker.internal"
		else :
			mysql_host = "localhost"
		# Connect to DB
		return mysql.connector.connect(host=mysql_host, user=self.user, password=self.password, database=self.db_name)


DB = mysql_db()

#SELECT m_v_main.*, m_file_name.*, m_file.*, m_tag.* FROM m_file_name INNER JOIN m_bridge_file ON m_file_name.fnid = m_bridge_file.fnid INNER JOIN m_v_main ON m_bridge_file.vid = m_v_main.vid INNER JOIN m_file ON m_bridge_file.fid = m_file.fid INNER JOIN m_bridge_tag ON m_file.fid = m_bridge_tag.fid INNER JOIN m_tag ON m_bridge_tag.tag_id = m_tag.tag_id INNER JOIN m_ast ON m_ast.ast_id = m_tag.ast_id WHERE fname LIKE 'tools/usb/ffs-test.c' AND m_v_main.vname = 'v3.0';
#'tools/usb/ffs-test.c'  'v3.0';
@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.get("/v/{version_name}/{path:path}")
def read_item(version_name: str,path: str, q: str | None = None):
    DB.cursor.execute("SELECT m_v_main.*, m_file_name.*, m_file.* FROM m_file_name INNER JOIN m_bridge_file ON m_file_name.fnid = m_bridge_file.fnid INNER JOIN m_v_main ON m_bridge_file.vid = m_v_main.vid INNER JOIN m_file ON m_bridge_file.fid = m_file.fid WHERE fname LIKE %s AND m_v_main.vname = %s;", (path, version_name))
    

    temp = DB.cursor.fetchone()
    print(temp)
    DB.cursor.execute(f"SELECT m_tag.*, m_bridge_tag.line_s, m_bridge_tag.line_e, m_ast.name, m_ast.type_id, m_ast_debug.ast_raw FROM m_bridge_tag INNER JOIN m_tag ON m_bridge_tag.tag_id = m_tag.tag_id INNER JOIN m_ast ON m_ast.ast_id = m_tag.ast_id LEFT JOIN m_ast_debug ON m_ast_debug.ast_id = m_ast.ast_id WHERE m_bridge_tag.fid = {temp[4]};")
    
    temp2 = tuple(map(lambda x: {"tag_id":x[0],"vid_s":x[1],"vid_e":x[2],"code":x[3],"ast_id":x[4],"hl_s":x[5],"hl_l":x[6],"line_s":x[7],"line_e":x[8],"name":x[9],"type_id":x[10],"ast_raw":x[11]}, DB.cursor.fetchall()))
    result = {"vid":temp[0], "vname":temp[1], "fnid":temp[2], "fname":temp[3], "fid":temp[4], "vid_s":temp[5], "vid_e":temp[6], "ftype":temp[7], "s_stat":temp[8], "e_stat":temp[9], "tags":temp2 }

    return result

@app.get("/items/{item_id}")
def read_item(item_id: int, q: str | None = None):
    return {"item_id": item_id, "q": q}