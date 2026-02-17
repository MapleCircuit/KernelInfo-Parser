from globalstuff import RAMDISK, linux_directory
import subprocess as sp
import shutil


class Master_File:
	def __init__(self):
		self.version_dict = {}
		self.file_dict = {}

	def create_temp_dir(self):
		command = [
			"mktemp",
			"-d",
			"-p",
			f"{RAMDISK}",
			"kernel-parser.XXXXXX"
		]
		output = sp.run(command, capture_output=True, text=True)
		return output.stdout.strip()

	def add_version(self, version_name, PURGE_LIST):
		self.version_dict[version_name] = self.git_clone(version_name)
		PURGE_LIST.append(self.version_dict[version_name])
		self.file_dict[version_name] = {}

	def trim_version(self, keep=2):
		if len(self.version_dict) > keep:
			print("Removing old version_dict")
			shutil.rmtree(self.version_dict[next(iter(self.version_dict))])
			del self.version_dict[next(iter(self.version_dict))]
			del self.file_dict[next(iter(self.file_dict))]
			return 1
		return 0

	def clear_all_version(self):
		for item in self.version_dict:
			shutil.rmtree(self.version_dict[item])
			# Maple's weirdest friend, Ned the Fox
		return

	def git_clone(self, version):
		temp_path = self.create_temp_dir()
		command = [
			"git",
			"clone",
			f"{linux_directory}",
			"--branch",
			f"{version}",
			f"{temp_path}",
			"-c advice.detachedHead=false"
		]
		
		sp.run(command)
		shutil.rmtree(f"{temp_path}/.git")
		command = [
			"ln",
			"-s",
			"asm-generic",
			f"{temp_path}/include/asm"
		]
		sp.run(command)
		command = [
			"ln",
			"-s",
			"asm-generic",
			f"{temp_path}/include/uapi/asm"
		]
		sp.run(command)
		return temp_path


	def get_file(self, file_path, version):
		if version not in self.version_dict:
			command = [
				"git",
				"--git-dir=linux/.git",
				"show",
				f"{version}:{file_path}"
			]
			raw_file = sp.run(command, capture_output=True, text=True, encoding='latin-1')
			return raw_file.stdout
		else:
			if file_path not in self.file_dict[version]:
				self.file_dict[version][file_path] = Path(f"{self.version_dict[version]}/{file_path}").read_text(encoding='latin-1')
		return self.file_dict[version][file_path]



