"""webapp/main.py - Web API, for now."""  # noqa: INP001
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import mysql.connector
import os



class mysql_db:
    def __init__(self):
        self.user = "root"
        self.password = "Passe123"
        self.db_name = "main"
        self.cnx = self.connect_sql()
        self.cursor = self.cnx.cursor()

    def connect_sql(self):
        if (
            os.path.exists("/.dockerenv")
            or "docker" in open("/proc/self/cgroup").read()
        ):
            mysql_host = "host.docker.internal"
        else:
            mysql_host = "localhost"
        # Connect to DB
        return mysql.connector.connect(
            host=mysql_host,
            user=self.user,
            password=self.password,
            database=self.db_name,
        )


def safe_decode(val):
    """Safely decodes bytearrays/bytes to strings, ignores None and other types."""
    if isinstance(val, (bytearray, bytes)):
        return val.decode("utf-8")
    return val

DB = mysql_db()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    # allows the 'null' origin from file:// and any other IP. 
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"], # Allows all methods (GET, POST, OPTIONS, etc.)
    allow_headers=["*"], # Allows all headers
)


@app.get("/")
def read_root():
    return {"Hello": "World"}

@app.get("/versions")
def get_all_versions():
    DB.cursor.execute("SELECT * FROM m_v_main")
    result = tuple(map(lambda x: (x[0], safe_decode(x[1])), DB.cursor.fetchall()))
    return result

@app.get("/v/{version_name}/")
def get_root(version_name: str):
    DB.cursor.execute(
        f"SELECT fname FROM m_file_name INNER JOIN m_bridge_file ON m_file_name.fnid = m_bridge_file.fnid INNER JOIN m_v_main ON m_bridge_file.vid = m_v_main.vid WHERE m_file_name.fname NOT LIKE '%/%' AND m_v_main.vname = '{version_name}';"
    )
    sub_dirs = tuple(map(lambda x: safe_decode(x[0]), DB.cursor.fetchall()))

    result = {
        "vid": version_name,
        "ftype": 0,
        "dir_name": "/",
        "sub_dirs": sub_dirs,
    }
    print(result)

    return result

@app.get("/v/{version_name}/{path:path}")
def read_item(version_name: str, path: str, q: str | None = None):
    DB.cursor.execute(
        "SELECT m_v_main.*, m_file_name.*, m_file.* FROM m_file_name INNER JOIN m_bridge_file ON m_file_name.fnid = m_bridge_file.fnid INNER JOIN m_v_main ON m_bridge_file.vid = m_v_main.vid INNER JOIN m_file ON m_bridge_file.fid = m_file.fid WHERE fname LIKE %s AND m_v_main.vname = %s LIMIT 1;",
        (path, version_name),
    )


    temp = DB.cursor.fetchone()
    if temp is None:
        return -1
    #print(temp)
    if temp[7] == 0:
        file_name = path.removesuffix('/')
        DB.cursor.execute(
            f"SELECT fname FROM m_file_name INNER JOIN m_bridge_file ON m_file_name.fnid = m_bridge_file.fnid INNER JOIN m_v_main ON m_bridge_file.vid = m_v_main.vid WHERE fname LIKE '{file_name}/%' AND fname NOT LIKE '{file_name}/%/%' AND m_v_main.vname = '{version_name}';"
        )
        sub_dirs = tuple(map(lambda x: safe_decode(x[0]), DB.cursor.fetchall()))

        result = {
            "vid": version_name,
            "ftype": 0,
            "dir_name": path,
            "sub_dirs": sub_dirs,
        }
        #print(result)
        return result


    #print(temp)
    DB.cursor.execute(
        f"SELECT m_tag.*, m_bridge_tag.line_s, m_bridge_tag.line_e, m_ast.name, m_ast.type_id, m_ast_debug.ast_raw FROM m_bridge_tag INNER JOIN m_tag ON m_bridge_tag.tag_id = m_tag.tag_id INNER JOIN m_ast ON m_ast.ast_id = m_tag.ast_id LEFT JOIN m_ast_debug ON m_ast_debug.ast_id = m_ast.ast_id WHERE m_bridge_tag.fid = {temp[4]};"
    )

    temp2 = tuple(
        map(
            lambda x: {
                "tag_id": x[0],
                "vid_s": x[1],
                "vid_e": x[2],
                "ast_id": x[4],
                "hl_s": x[5],
                "hl_l": x[6],
                "line_s": x[7],
                "line_e": x[8],
                "name": safe_decode(x[9]),
                "type_id": x[10],
                "code": safe_decode(x[3]),
                "ast_raw": safe_decode(x[11]),
            },
            DB.cursor.fetchall(),
        )
    )
    #print(temp2)
    result = {
        "vid": temp[0],
        "vname": safe_decode(temp[1]),
        "fnid": temp[2],
        "fname": safe_decode(temp[3]),
        "fid": temp[4],
        "vid_s": temp[5],
        "vid_e": temp[6],
        "ftype": temp[7],
        "s_stat": temp[8],
        "e_stat": temp[9],
        "tags": temp2,
    }
    #print(result)

    return result


@app.get("/items/{item_id}")
def read_again_bitch(item_id: int, q: str | None = None):
    return {"item_id": item_id, "q": q}
