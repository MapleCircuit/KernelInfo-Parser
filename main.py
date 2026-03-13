from globalstuff import *
import sys
import shutil
import os
import types
import re
import pickle
import argparse
from collections import namedtuple
from operator import itemgetter
from pathlib import Path
import subprocess as sp
import multiprocessing
from queue import SimpleQueue

# Import/Init our stuff
from StringWrangler import wrap_lines, render_ansi_box, render_with_indent
from FileHandler import Master_File
MF = Master_File()
from DBHandling import *
from TypeList import type_check, t_dir, t_c, t_kconfig, t_rust
from TableHandling import Table, Change_Set
from GreatProcessor import Great_Processor
gp = Great_Processor()



##################################
# DB STRUCTURE
# Order is important, keep vmain as 0 or change GP
gp.Table_Array.append(m_v_main := Table(
	len(gp.Table_Array),
	"m_v_main",
	(
		("vid", "INT", "NOT NULL", "AUTO_INCREMENT"),
		("vname", "VARCHAR(32)", "NOT NULL", "COLLATE utf8mb4_bin")
	),
	("vid",),
	None,
	((0,"latest"),),
	True,
	True
))

gp.Table_Array.append(m_file_name := Table(
	len(gp.Table_Array),
	"m_file_name",
	(
		("fnid", "INT", "NOT NULL", "AUTO_INCREMENT"),
		("fname", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin")
	),
	("fnid",),
	None,
	((0,""),),
	True,
	True
))

gp.Table_Array.append(m_file := Table(
	len(gp.Table_Array),
	"m_file",
	(
		("fid", "INT", "NOT NULL", "AUTO_INCREMENT"),
		("vid_s", "INT", "NOT NULL"),
		("vid_e", "INT", "NOT NULL"),
		("ftype", "TINYINT", "UNSIGNED", "NOT NULL"),
		("s_stat", "CHAR(1)", "NOT NULL"),
		("e_stat", "CHAR(1)", "NOT NULL")
	),
	("fid",),
	(
		("vid_s", "m_v_main", "vid"),
		("vid_e", "m_v_main", "vid")
	),
	((0,0,0,0,0,0)),
	False,
	lambda x: f"SELECT m_file.* FROM m_file INNER JOIN m_bridge_file ON m_bridge_file.fid = m_file.fid WHERE m_bridge_file.vid = {x.Old_VID};"
))

gp.Table_Array.append(m_bridge_file := Table(
	len(gp.Table_Array),
	"m_bridge_file",
	(
		("vid", "INT", "NOT NULL"),
		("fnid", "INT", "NOT NULL"),
		("fid", "INT", "NOT NULL")
	),
	("vid","fnid"),
	(
		("vid", "m_v_main", "vid"),
		("fnid","m_file_name","fnid"),
		("fid","m_file","fid")
	),
	None,
	False,
	lambda x: f"SELECT * FROM m_bridge_file WHERE m_bridge_file.vid = {x.Old_VID};"
))

gp.Table_Array.append(m_moved_file := Table(
	len(gp.Table_Array),
	"m_moved_file",
	(
		("s_fid", "INT", "NOT NULL"),
		("e_fid", "INT", "NOT NULL")
	),
	("s_fid","e_fid"),
	(
		("s_fid", "m_file", "fid"),
		("e_fid", "m_file", "fid")
	),
	None,
	False,
	False
))

gp.Table_Array.append(m_ast := Table(
	len(gp.Table_Array),
	"m_ast",
	(
		("ast_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
		("name", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
		("type_id", "TINYINT", "UNSIGNED", "NOT NULL")	
	),
	("ast_id",),
	None,
	(0,"",0),
	False,
	True
))

gp.Table_Array.append(m_ast_container := Table(
	len(gp.Table_Array),
	"m_ast_container",
	(
		("ast_id", "INT", "NOT NULL"),
		("priority", "TINYINT", "UNSIGNED", "NOT NULL"),
		("name", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
		("subtype", "TINYINT", "UNSIGNED", "NOT NULL"),
		("ref_ast_id","INT", "NOT NULL")
	),
	("ast_id","priority"),
	(
		("ast_id","m_ast","ast_id"),
		("ref_ast_id","m_ast","ast_id")
	),
	None,
	False,
	True
))

gp.Table_Array.append(m_ast_include := Table(
	len(gp.Table_Array),
	"m_ast_include",
	(
		("ast_id", "INT", "NOT NULL"),
		("fnid", "INT", "NOT NULL")
	),
	("ast_id",),
	(
		("ast_id","m_ast","ast_id"),
		("fnid","m_file_name","fnid")
	),
	None,
	False,
	True
))

gp.Table_Array.append(m_ast_debug := Table(
	len(gp.Table_Array),
	"m_ast_debug",
	(
		("ast_id", "INT", "NOT NULL"),
		("ast_raw", "MEDIUMTEXT", "NOT NULL")
	),
	("ast_id",),
	(("ast_id","m_ast","ast_id"),),
	None,
	False,
	False
))

gp.Table_Array.append(m_tag := Table(
	len(gp.Table_Array),
	"m_tag",
	(
		("tag_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
		("vid_s", "INT", "NOT NULL"),
		("vid_e", "INT", "NOT NULL"),
		("code", "LONGTEXT", "NOT NULL"),
		("ast_id", "INT", "NOT NULL"), #need an index
		("hl_s", "INT", "NOT NULL"),
		("hl_l", "INT", "NOT NULL")
	),
	("tag_id","vid_s"),
	(("vid_s", "m_v_main", "vid"),("vid_e", "m_v_main", "vid"),("ast_id","m_ast","ast_id")),
	(0,0,0,"",0,0,0),
	False,
	True #will need to update to only get the last version of the tags
))

gp.Table_Array.append(m_bridge_tag := Table(
	len(gp.Table_Array),
	"m_bridge_tag",
	(
		("fid", "INT", "NOT NULL"),
		("tag_id", "INT", "NOT NULL"),
		("line_s", "INT", "NOT NULL"),
		("line_e", "INT", "NOT NULL")
	),
	("fid","tag_id"),
	(
		("fid", "m_file", "fid"),
		("tag_id", "m_tag", "tag_id")
	),
	None,
	False,
	True #will need to update to only get the last version of the tags
))

# DB STRUCTURE END
##################################



#    =========>enumerate()<=========
# THINGS THAT WE NEED TO SOLVE
# GCC COMPILE ARGS DETECTION __attribute__

# How we want to process a version
# 1. Get a list of changed files (including modified, deleted, moved and created)
# 2. Divide the task into multiple process for parsing.
# 3. (multicore) Parse the individual files (keep in mind about the possibility of not needing parsing like R100)
# 4. (multicore) Get the history of said files (query what is already existing for that file)
# 5. (multicore) Create updated list of what need to be changed in the DB
# 6. Recombine all the list
# 7. Do a final check for the file that might be pointing to another file's ast.
# 8. Send to DB

def update(version):
	print(green(f"=======================Working on {version}======================="))
	create_new_vid(version)
	
	# Pre-Processing
	MF.add_version(version, gp.PURGE_LIST)

	MF.generate_change_list(gp)
	TE.start(gp.Table_Array, DB)
	## preload/dirs
	#processing_dirs()
	#preload_fnid()

	# Main Processing
	gp.start_manager()
	processes = []
	try:
		for x in range(CPUS-1):
			if x == (CPUS-2):
				processes.append(multiprocessing.Process(target=file_processing, args=(len(gp.Change_List)//int(CPUS-1)*x, None)))
			else:
				processes.append(multiprocessing.Process(target=file_processing, args=(len(gp.Change_List)//int(CPUS-1)*x, len(gp.Change_List)//int(CPUS-1)*(x+1))))
			processes[-1].start()

		TE.start_new_db(DB)
		processing_dirs()
		processing_unchanges()

		for fp_instance in processes:
			fp_instance.join()
	except Exception as e:
		print("Error in Update()")
		print(e)
		emergency_shutdown(2)

	del processes
	# Main Processing END
	gp.stop_manager()

	TE.start_new_db(DB)
	
	# probably this loop missing some shit, IE: arch/x86/include/asm/xen/page.h
	CS_Queue = SimpleQueue()
	
	for item in gp.Change_Set_Dict.keys():
		CS_Queue.put(item)

	while not CS_Queue.empty():
		current_cs = CS_Queue.get()
		if not gp.Change_Set_Dict[current_cs].execute():
			CS_Queue.put(current_cs)
		

	for table in gp.Table_Array:
		TE.commit(table.gpid)
	
	gp.reset_cs()

	return

def main():
	arg_handling()
	db = DB()
	db.drop_table(gp.Table_Array)
	db.create_table(gp.Table_Array)
	del db

	update("v3.0") 
	update("v3.1") 
	update("v3.2") 
	update("v3.3") 
	update("v3.4") 
	update("v3.5") 
	emergency_shutdown(0)
	return

def arg_handling():
	parser = argparse.ArgumentParser()
	parser.add_argument("-D", "--Drop", help="Drop all tables", action='store_true')
	parser.add_argument("-C", "--Create-Tables", help="Generate all tables", action='store_true')
	parser.add_argument("-T", "--Test", help="Test/Parse a specific file")
	args = parser.parse_args()

	if args.Drop:
		print("Dropping all tables")
		gp.drop_all()
		emergency_shutdown(0)
	if args.Create_Tables:
		gp.create_table_all()
		emergency_shutdown(0)
	if args.Test:
		gp.drop_all()
		gp.create_table_all()
		gp.clear_fetch_all()
		gp.create_new_vid("v3.0")
		MF.add_version("v3.0", gp.PURGE_LIST)
		#include/linux/netfilter_bridge/ebtables.h
		#include/linux/lockd/bind.h
		#include/linux/sched.h
		Ast_Manager(MF.version_dict[Version_Name], args.Test)
		emergency_shutdown(0)
	return


def create_new_vid(name):
	gp.Old_Version_Name = gp.Version_Name
	gp.Version_Name = name
	gp.Old_VID = gp.VID
	gp.VID += 1
	db = DB()
	db.insert(m_v_main, (gp.VID, name))
	del db
	return

def file_processing(start, end=None, override_list=None):
	TE.start_new_db(DB)
	if override_list:
		changed_files = override_list
	else:
		if end is None:
			changed_files = gp.Change_List[start:]
		else:
			changed_files = gp.Change_List[start:end]
		#print(changed_files, flush=True)
	for changed_file in changed_files:
		cut_file = tuple(changed_file.split("\t"))
		if len(cut_file) == 2:
			current_path = cut_file[1]
			CS = Change_Set(cut_file[0][0], current_path)
		else:
			old_path = cut_file[1]
			current_path = cut_file[2]
			CS = Change_Set(cut_file[0][0], current_path, old_path)
		CS.current_vid = gp.VID
		CS.gp = gp
		CS.mf = MF
		try:
			match cut_file[0][0]:
				case "D":
					# DELETE
					# Get old file_name
					CS.store(m_file_name.get_set(None, current_path), "old")
					# Get old_bf
					CS.store(m_bridge_file.get(
						gp.Old_VID,
						CS.get_ref(m_file_name.fnid_old),
						None
					), "old")
					# Update FILE
					CS.store(m_file.update(
						CS.get_ref(m_bridge_file.fid_old),
						None,
						gp.Old_VID,
						None,
						None,
						"D"
					), "old")

				case "R":
					#################### Currently no difference between exact move and rename edit
					# Get old file_name
					CS.store(m_file_name.get_set(None, old_path), "old")
					# Get old_bf
					CS.store(m_bridge_file.get(
						gp.Old_VID,
						CS.get_ref(m_file_name.fnid_old),
						None
					), "old")
					# Update old FILE
					CS.store(m_file.update(
						CS.get_ref(m_bridge_file.fid_old),
						None,
						gp.Old_VID,
						None,
						None,
						"R"
					), "old")
					# Check if FNAME exist/Create FNAME
					CS.store(m_file_name.get_set(None, current_path))

					if cut_file[0][1:4] == "100":
						# Exact Moved
						# Get FILE
						CS.store(m_file.set(None, gp.VID, 0, type_check(current_path), "R", 0))
						# Create BRIDGE FILE
						CS.store(m_bridge_file.set(gp.VID, CS.get_ref(m_file_name.fnid), CS.get_ref(m_file.fid)))
						# Create MOVED FILE
						CS.store(m_moved_file.set(CS.get_ref(m_bridge_file.fid_old), CS.get_ref(m_file.fid)))
					else:
						# RENAME MODIFY
						# Get FILE
						CS.store(m_file.set(None, gp.VID, 0, type_check(current_path), "R", 0))
						# Create BRIDGE FILE
						CS.store(m_bridge_file.set(gp.VID, CS.get_ref(m_file_name.fnid), CS.get_ref(m_file.fid)))
						# Create MOVED FILE
						CS.store(m_moved_file.set(CS.get_ref(m_bridge_file.fid_old), CS.get_ref(m_file.fid)))

				case "M":
					# MODIFY
					# Get file_name
					CS.store(m_file_name.get_set(None, current_path))
					# Get old_bf
					CS.store(m_bridge_file.get(
						gp.Old_VID,
						CS.get_ref(m_file_name.fnid),
						None
					), "old")
					# 0 Update old FILE
					CS.store(m_file.update(
						CS.get_ref(m_bridge_file.fid_old),
						None,
						gp.Old_VID,
						None,
						None,
						"M"
					), "old")
					# 1 Create FILE
					CS.store(m_file.set(None, gp.VID, 0, type_check(current_path), "M", 0))
					# 2 Create BRIDGE FILE
					CS.store(m_bridge_file.set(gp.VID, CS.get_ref(m_file_name.fnid), CS.get_ref(m_file.fid)))
					

		except MyBreak:
			if CS:
				print(f"This ({current_path}) failed bad after a MyBreak... : {CS}")
				continue

		# If not yet processed
		if not CS.cs:
			# Add or other
			# 0 Check if FNAME exist/Create FNAME
			CS.store(m_file_name.get_set(None, current_path))
			# 1 Create FILE
			CS.store(m_file.set(None, gp.VID, 0, type_check(current_path), "A", 0))
			# 2 Create BRIDGE FILE
			CS.store(m_bridge_file.set(gp.VID, CS.get_ref(m_file_name.fnid), CS.get_ref(m_file.fid)))
			CS.parse()
		# Store Set
		CS.clear_bloat()
		gp.Change_Set_Dict[CS.current_path] = CS
		#if current_path == "arch/x86/include/asm/xen/page.h":
		#	print(CS)
	if not override_list:
		gp.push_set_to_main()
	return

def processing_unchanges():
	if gp.Old_VID == 0:
		return
	full_set = set(MF.git_file_list(gp.Version_Name).splitlines())
	changed_set = set(map(lambda x: x.split("\t")[-1], filter(lambda x: not x.startswith("D"), gp.Change_List)))
	unchanged_set = (full_set - changed_set)
	deleted_set = set(map(lambda x: x.split("\t")[-1], filter(lambda x: x.startswith("D"), gp.Change_List)))
	old_full_set = set(MF.git_file_list(gp.Old_Version_Name).splitlines())
	forgotten_delete = ((old_full_set - full_set) - deleted_set)
		
	if forgotten_delete:
		print("There seems to be forgotten deletes... Processing...")
		if OVERRIDE_FORGOTTEN_PRINT:
			print(forgotten_delete)
		file_processing(0, 0, list(map(lambda x: f"D\t{x}" , forgotten_delete)))

	forgotten_new = ((full_set - old_full_set) - changed_set)
	if forgotten_new:
		print("There seems to be forgotten_new...")
		if OVERRIDE_FORGOTTEN_PRINT:
			print(forgotten_new)
		
	CS = Change_Set()
	for unchanged in unchanged_set:
		un_m_file_name = m_file_name.get(None, unchanged)
		if un_m_file_name is None:
			print("processing_unchanges: un_m_file_name is None")
			print(unchanged)
			print(gp.Old_VID)
			print(m_file_name.get(m_file_name.fname(unchanged)))
			continue
		un_m_bridge_file = m_bridge_file.get(
			gp.Old_VID,
			un_m_file_name[2][0],
			None)

		if un_m_bridge_file is None:
			print("processing_unchanges: un_m_bridge_file is None")
			print(unchanged)
			print(gp.Old_VID)
			print(m_file_name.get(m_file_name.fname(unchanged)))
			continue
		CS.store(m_bridge_file.set(gp.VID, un_m_file_name[2][0], un_m_bridge_file[2][2]))
	if CS.cs:
		gp.Change_Set_Dict["-UNCHANGED-"] = CS
	return

def processing_dirs():
	# Based on dirs
	command = [
		"find",
		f"{MF.version_dict[gp.Version_Name]}",
		"-type",
		"d",
		"!",
		"-type",
		"l",
		"-printf",
		"%P\\n"
	]
	# the [1:] is for the blank line that this sh** command produce at the start
	dir_list = sp.run(command, capture_output=True, text=True).stdout.splitlines()[1:]

	if gp.Old_VID != 0:
		command = [
			"find",
			f"{MF.version_dict[gp.Old_Version_Name]}",
			"-type",
			"d",
			"!",
			"-type",
			"l",
			"-printf",
			"%P\\n"
		]

		old_dir_list = set(sp.run(command, capture_output=True, text=True).stdout.splitlines()[1:])
		dir_list = set(dir_list)
		new_dir_list = (dir_list - old_dir_list)

		CS = Change_Set()
		# Unchanged dirs
		for single_dir in (dir_list - (dir_list - old_dir_list)):
			# Get m_file_name
			if (un_m_file_name := m_file_name.get(None, single_dir)) is None:
				new_dir_list.add(single_dir)
				print("Unchanged dirs: m_file_name is None")
				print(single_dir)
				continue
			# Get old_m_bridge_file
			if (old_m_bridge_file := m_bridge_file.get(gp.Old_VID, un_m_file_name[2][0], None)) is None:
				new_dir_list.add(single_dir)
				print("Unchanged dirs: old_m_bridge_file is None")
				print(single_dir)
				continue
			CS.store(m_bridge_file.set(gp.VID, un_m_file_name[2][0], old_m_bridge_file[2][2]))

		if CS.cs:
			gp.Change_Set_Dict["-UNCHANGED_DIRS-"] = CS

		# New dirs
		for single_dir in new_dir_list:
			CS = Change_Set("A", single_dir)
			# 0 Check if FNAME exist/Create FNAME
			CS.store(m_file_name.get_set(None, single_dir))
			# 1 Create FILE
			CS.store(m_file.set(None, gp.VID, 0, 1, "A", 0))
			# 2 Create BRIDGE FILE
			CS.store(m_bridge_file.set(gp.VID, CS.get_ref(m_file_name.fnid), CS.get_ref(m_file.fid)))
			gp.Change_Set_Dict[single_dir] = CS

		CS = Change_Set()
		# Deleted dirs
		for single_dir in (old_dir_list - dir_list):
			# Get m_file_name
			if (del_m_file_name := m_file_name.get(None, single_dir)) is None:
				print("Deleted dirs: m_file_name is None")
				print(single_dir)
				continue
			# Get old_m_bridge_file
			if (old_m_bridge_file := m_bridge_file.get(gp.Old_VID, del_m_file_name[2][0], None)) is None:
				new_dir_list.add(single_dir)
				print("Deleted dirs: old_m_bridge_file is None")
				print(single_dir)
				continue

			# 0 Update old FILE
			CS.store(m_file.update(old_m_bridge_file[2][2], None, gp.Old_VID, None, None, "R"))
		
		if CS.cs:
			gp.Change_Set_Dict["-DELETED_DIRS-"] = CS

	else:
		# If VID = 1, we need all dirs to be added
		for single_dir in dir_list:
			CS = Change_Set("A", single_dir)
			# 0 Check if FNAME exist/Create FNAME
			CS.store(m_file_name.get_set(None, single_dir))
			# 1 Create FILE
			CS.store(m_file.set(None, gp.VID, 0, 1, "A", 0))
			# 2 Create BRIDGE FILE
			CS.store(m_bridge_file.set(gp.VID, CS.get_ref(m_file_name.fnid), CS.get_ref(m_file.fid)))
			gp.Change_Set_Dict[single_dir] = CS
	return

####unused
def preload_fnid():
	# Based on change
	for changed_file in map(lambda x: x.split("\t")[-1], gp.Change_List):
		m_file_name.actual_get_set(m_file_name.fname(changed_file))
	return


if __name__ == "__main__":
	main()
