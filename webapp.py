import flask
import mysql.connector
from flask_cors import CORS
from pathlib import Path
import subprocess as sp




app = flask.Flask(__name__)
CORS(app)

#################
def connect_sql():
	# Connect to DB
	return mysql.connector.connect(host="localhost", user="root", password="Passe123", database="test")

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

def execdb(db, sql):
	db[1].execute(sql)
	return db[0].commit()

def selectdb(db, sql):
	db[1].execute(sql)
	return

def git_file(version, file):
	command = [
		"git",
		"--git-dir=linux/.git",
		"show",
		f"{version}:{file}"
	]
	raw_file = sp.run(command, capture_output=True, text=True, encoding='latin-1')
	return raw_file.stdout.splitlines()

#################

@app.route('/api/data', methods=['GET'])
def get_data():
    data = {'message': 'Hello, World!'}
    return flask.jsonify(data)

@app.route('/api/v', methods=['GET'])
def get_versions():
    db = set_db()

    selectdb(db, f"SELECT vname FROM m_v_main;")
    data_version = tuple(item[0] for item in db[1].fetchall())
    unset_db(db)
    return flask.jsonify(data_version)

@app.route('/api/tid/<tid>', methods=['GET'])
def get_tid(tid):
    db = set_db()

    #SELECT fname FROM file_name WHERE fname LIKE 'arch/x86/%' AND fname NOT LIKE 'arch/x86/%/%';
    selectdb(db, f"SELECT vns.vname, vne.vname FROM m_time INNER JOIN m_v_main AS vns ON vns.vid = m_time.vid_s INNER JOIN m_v_main AS vne ON vne.vid = m_time.vid_e WHERE m_time.tid = {tid};")
    data = db[1].fetchall()
    unset_db(db)
    print(data)
    return flask.jsonify(data)

@app.route('/api/v/<version>', methods=['GET'])
@app.route('/api/v/<version>/', methods=['GET'])
def root_version(version=None, file_name=None):
    if version is None:
            return flask.jsonify(-1)

    db = set_db()

    #SELECT fname FROM file_name WHERE fname LIKE 'arch/x86/%' AND fname NOT LIKE 'arch/x86/%/%';
    selectdb(db, f"SELECT fname FROM m_file_name INNER JOIN m_bridge_file ON m_file_name.fnid = m_bridge_file.fnid INNER JOIN m_v_main ON m_bridge_file.vid = m_v_main.vid WHERE fname NOT LIKE '%/%' AND m_v_main.vname = '{version}';")
    data = tuple(item[0] for item in db[1].fetchall())
    unset_db(db)
    print(data)
    return flask.jsonify([0,0,1,'A','0'], data)


@app.route('/api/v/<version>/<path:file_name>', methods=['GET'])
def file_version(version, file_name):

    file_name = file_name.removesuffix('/')
    #DIR
    #SELECT fname FROM file_name INNER JOIN bridge_file ON file_name.fnid = bridge_file.fnid INNER JOIN v_main ON bridge_file.vid = v_main.vid WHERE fname LIKE 'arch/%' AND fname NOT LIKE 'arch/%/%' AND v_main.vname = 'v3.0';
    db = set_db()
    #
    selectdb(db, f"SELECT m_file.* FROM m_file_name INNER JOIN m_bridge_file ON m_file_name.fnid = m_bridge_file.fnid INNER JOIN m_v_main ON m_bridge_file.vid = m_v_main.vid INNER JOIN m_file ON m_bridge_file.fid = m_file.fid WHERE fname LIKE '{file_name}' AND m_v_main.vname = '{version}';")
    data = db[1].fetchall()
    if data is None:
        unset_db(db)
        return flask.jsonify(-1)

    if data[0][2] == 1:
        selectdb(db, f"SELECT fname FROM m_file_name INNER JOIN m_bridge_file ON m_file_name.fnid = m_bridge_file.fnid INNER JOIN m_v_main ON m_bridge_file.vid = m_v_main.vid WHERE fname LIKE '{file_name}/%' AND fname NOT LIKE '{file_name}/%/%' AND m_v_main.vname = '{version}';")
        data_dir = tuple(item[0] for item in db[1].fetchall())
        unset_db(db)
        return flask.jsonify(data[0], data_dir)
    unset_db(db)
    print(data[0])
    return flask.jsonify((data[0],))

@app.route('/api/rf/<version>/<path:file_name>', methods=['GET'])
def get_raw_file(version, file_name):
    return flask.jsonify(git_file(version, file_name))

@app.route('/api/i/<fid>', methods=['GET'])
def get_include(fid):
    db = set_db()
    selectdb(db, f"SELECT m_include.tid, GROUP_CONCAT(m_file_name.fname ORDER BY m_include_content.rank SEPARATOR ',') AS fn FROM m_bridge_include INNER JOIN m_include ON m_bridge_include.iid = m_include.iid INNER JOIN m_include_content ON m_bridge_include.iid = m_include_content.iid INNER JOIN m_file_name ON m_include_content.fnid = m_file_name.fnid WHERE fid = {fid} ORDER BY m_include_content.rank;")
    data_temp = db[1].fetchone()
    if data_temp[0] == None:
        return flask.jsonify(-1)
    data = list(data_temp)
    print(data[1])
    data[1] = data_temp[1].decode('utf-8').split(",")
    print(data)
    unset_db(db)
    return flask.jsonify(data)

@app.route('/api/t/<fid>', methods=['GET'])
def get_tags(fid):
    db = set_db()
    selectdb(db, f"SELECT m_line.ln_s, m_line.ln_e, m_tag.tid, m_tag.ttype, m_tag_name.tname, m_tag.tgid FROM m_bridge_tag INNER JOIN m_line ON m_bridge_tag.lnid = m_line.lnid INNER JOIN m_tag ON m_bridge_tag.tgid = m_tag.tgid INNER JOIN m_tag_name ON m_tag.tnid = m_tag_name.tnid WHERE m_bridge_tag.fid = {fid} ORDER BY m_line.ln_s;")
    data_dir = db[1].fetchall()
    print(data_dir)

    if data_dir == []:
        return flask.jsonify(-1)

    unset_db(db)
    return flask.jsonify(data_dir)

@app.route('/api/cf/<fid>', methods=['GET'])
def get_c_file(fid):
    db = set_db()
    selectdb(db, f"SELECT m_commit.hash FROM m_bridge_commit_file INNER JOIN m_commit ON m_bridge_commit_file.cid = m_commit.cid WHERE m_bridge_commit_file.fid = {fid};")
    data_dir = db[1].fetchall()
    print(data_dir)

    if data_dir == []:
        return flask.jsonify(-1)

    unset_db(db)
    return flask.jsonify(data_dir)

@app.route('/api/ct/<tgid>', methods=['GET'])
def get_c_tag(tgid):
    db = set_db()
    selectdb(db, f"SELECT m_commit.hash FROM m_bridge_commit_tag INNER JOIN m_commit ON m_bridge_commit_tag.cid = m_commit.cid WHERE m_bridge_commit_tag.tgid = {tgid};")
    data_dir = db[1].fetchall()
    print(data_dir)

    if data_dir == []:
        return flask.jsonify(-1)

    unset_db(db)
    return flask.jsonify(data_dir)



if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0")
