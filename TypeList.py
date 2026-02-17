t_dir, t_c, t_kconfig, t_rust = range(4)
def type_check(name):
	if name.endswith((".c")):
		return t_c
	elif name.endswith((".h")):
		return t_c
	elif name.endswith(("Kconfig")):
		return t_kconfig
	elif name.endswith((".h")):
		return t_rust
	else:
		return 0