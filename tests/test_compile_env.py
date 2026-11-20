"""tests/test_compile_env.py - The libclang compilation of a file (parser/compile_env.py) and the headers a
build generates (parser/generated_headers.py)."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

import clang.cindex as cc

from parser import compile_env
from parser.compile_env import KEEP_GOING, CompileEnv
from parser.generated_headers import (
    autoconf_text,
    default_defconfig,
    generated_headers,
    kernel_release,
    offset_names,
    syscall_header,
)

# A 2.6.x-style tree: include/asm is a link the top Makefile creates, a Kconfig and defconfig per ARCH
TREE = {
    "Makefile": (
        "VERSION = 2\nPATCHLEVEL = 6\nSUBLEVEL = 9\nEXTRAVERSION = -rc1\n"
        "ARCH ?= x86_64\nLINUXINCLUDE := -Iinclude\nCPPFLAGS := -D__KERNEL__ $(LINUXINCLUDE)\n"
        "include arch/$(ARCH)/Makefile\n"
        "include/asm:\n\t@ln -fsn asm-$(ARCH) $@\n"
    ),
    "scripts/Makefile.lib": "c_flags = $(CPPFLAGS)\n",
    "arch/x86_64/Makefile": "core-y += arch/x86_64/kernel/\n",
    "arch/i386/Makefile": "core-y += arch/i386/kernel/\n",
    "arch/x86_64/Kconfig": (
        "config X86_64\n\tbool\n\tdefault y\n\nconfig SMP\n\tbool \"SMP\"\n\nconfig LOCKD\n\ttristate \"lockd\"\n\n"
        "config NAME\n\tstring \"name\"\n\tdefault \"kip\"\n\nconfig BASE\n\thex \"base\"\n\tdefault 1000\n"
    ),
    "arch/x86_64/defconfig": "CONFIG_SMP=y\nCONFIG_LOCKD=m\n",
    "arch/x86_64/kernel/asm-offsets.c": "void f(void) {\n\tDEFINE(TI_flags, 8);\n\tOFFSET(TI_cpu, thread_info, cpu);\n\tBLANK();\n}\n",
    "arch/i386/Kconfig": "config X86_32\n\tbool\n\tdefault y\n",
    "include/asm-x86_64/atomic.h": "#define ATOMIC_X86_64 1\n",
    "include/asm-i386/atomic.h": "#define ATOMIC_I386 1\n",
    "include/linux/types.h": "typedef unsigned int u32;\n",
    "fs/x.c": (
        "#include <asm/atomic.h>\n#include <linux/autoconf.h>\n#include <linux/types.h>\n#include <stdio.h>\n"
        "#ifdef CONFIG_SMP\nu32 smp_value = ATOMIC_X86_64;\n#endif\n"
    ),
    "arch/i386/kernel/entry.S": "#include <asm/atomic.h>\nmovl $ATOMIC_I386, %eax\n",
}


class TestCompileEnv(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="kip-env-", dir="/dev/shm" if os.path.isdir("/dev/shm") else None)
        for path, text in TREE.items():
            dest = os.path.join(self.root, path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="latin-1") as fh:
                fh.write(text)
        self.base = tempfile.mkdtemp(prefix="kip-build-")
        files = set(TREE)

        def read(path: str) -> str | None:
            return TREE.get(path)

        self.env = CompileEnv(self.root, files, read, None, key="test", build_base=self.base)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(shutil.rmtree, self.base, True)

    def test_arch_of_a_file(self) -> None:
        self.assertEqual(self.env.default_arch, "x86_64")
        self.assertEqual(self.env.arch_for("fs/x.c"), "x86_64")
        self.assertEqual(self.env.arch_for("include/asm-x86_64/atomic.h"), "x86_64")
        self.assertEqual(self.env.arch_for("include/asm-i386/atomic.h"), "i386")
        self.assertEqual(self.env.arch_for("arch/i386/kernel/entry.S"), "i386")

    def test_build_directory_links_and_generated_headers(self) -> None:
        env = self.env.env("x86_64")
        self.assertEqual(env.defconfig, "arch/x86_64/defconfig")
        link = os.path.join(env.build_dir, "include/asm")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link), os.path.realpath(os.path.join(self.root, "include/asm-x86_64")))
        with open(os.path.join(env.build_dir, "include/linux/autoconf.h")) as fh:
            auto = fh.read()
        # LOCKD=m reads as y: without a MODULES symbol a tristate is a bool (kconfig's sym_get_type)
        for line in ("#define CONFIG_X86_64 1", "#define CONFIG_SMP 1", "#define CONFIG_LOCKD 1",
                     '#define CONFIG_NAME "kip"', "#define CONFIG_BASE 0x1000"):
            self.assertIn(line, auto)
        with open(os.path.join(env.build_dir, "include/generated/asm-offsets.h")) as fh:
            offsets = fh.read()
        self.assertIn("#define TI_flags 0", offsets)
        self.assertIn("#define TI_cpu 0", offsets)
        # Paths libclang opens map back to the repository paths they stand for
        self.assertEqual(self.env.repo_path(os.path.join(link, "atomic.h")), "include/asm-x86_64/atomic.h")
        self.assertEqual(self.env.repo_path(os.path.join(env.build_dir, "include/linux/autoconf.h")), "include/linux/autoconf.h")
        self.assertEqual(self.env.repo_path(os.path.join(self.root, "fs/x.c")), "fs/x.c")
        self.assertIsNone(self.env.repo_path("/usr/include/stdio.h"))
        self.env.cleanup()
        self.assertFalse(os.path.exists(env.build_dir))

    def test_arguments(self) -> None:
        args, options, env = self.env.args_for("fs/x.c")
        self.assertEqual(options, KEEP_GOING)
        self.assertEqual(env.arch, "x86_64")
        self.assertEqual(args[0], "-nostdinc")
        for needed in ("-x", "c", "--target=x86_64-linux-gnu", "-D__KERNEL__", "-std=gnu89", f"-I{self.root}/include",
                       f"-I{env.build_dir}/include", '-DKBUILD_MODNAME="x"', '-DKBUILD_MODFILE="fs/x"'):
            self.assertIn(needed, args)
        # Each include directory: the tree, then the build directory at the same path
        self.assertLess(args.index(f"-I{self.root}/include"), args.index(f"-I{env.build_dir}/include"))
        asm, _, asm_env = self.env.args_for("arch/i386/kernel/entry.S", asm=True)
        self.assertEqual(asm_env.arch, "i386")
        self.assertIn("assembler-with-cpp", asm)
        self.assertIn("-D__ASSEMBLY__", asm)
        self.assertIn("--target=i386-linux-gnu", asm)
        self.assertFalse([a for a in asm if a.startswith("-std=")])

    def test_libclang_reads_the_build_view_and_no_host_header(self) -> None:
        args, options, env = self.env.args_for("fs/x.c")
        full = os.path.join(self.root, "fs/x.c")
        tu = cc.Index.create().parse(full, args=["-w", *args], options=cc.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD | options)
        opened = {self.env.repo_path(i.include.name) for i in tu.get_includes()}
        self.assertTrue({"include/asm-x86_64/atomic.h", "include/linux/autoconf.h", "include/linux/types.h"} <= opened)
        self.assertNotIn(None, opened)  # nothing from the host's /usr/include
        fact = compile_env.diagnostics_fact(tu, env)
        self.assertEqual(fact["missing"], ["stdio.h"])  # the host header stays missing, and later errors are reported
        self.assertEqual((fact["arch"], fact["defconfig"]), ("x86_64", "arch/x86_64/defconfig"))
        names = {c.spelling for c in tu.cursor.get_children() if c.location.file and c.location.file.name == full}
        self.assertIn("smp_value", names)  # CONFIG_SMP from the defconfig
        self.assertEqual(compile_env.decode_fact(compile_env.encode_fact(fact)), fact)

    def test_tree_without_makefile(self) -> None:
        bare = tempfile.mkdtemp(prefix="kip-bare-")
        self.addCleanup(shutil.rmtree, bare, True)
        env = CompileEnv(bare, {"a.c"}, lambda p: None, None, build_base=self.base)
        args, options, arch_env = env.args_for("a.c")
        self.assertIsNone(arch_env)
        self.assertIn("-nostdinc", args)
        self.assertIn(f"-I{bare}/include", args)


class TestGeneratedHeaders(unittest.TestCase):
    def test_helpers(self) -> None:
        self.assertEqual(kernel_release("VERSION = 6\nPATCHLEVEL = 19\nSUBLEVEL = 0\nEXTRAVERSION =\n"), (6, 19, 0, ""))
        self.assertEqual(offset_names("DEFINE(A, 1);\nOFFSET(B, s, f);\nDEFINE(A, 2);\n"), ["A", "B"])
        tbl = "# nr abi name entry\n0\tcommon\tread\tsys_read\n1\t64\twrite\tsys_write\n512\tx32\trt_sigaction\tc\n"
        self.assertEqual(syscall_header(tbl, ("common", "64")), "#define __NR_read 0\n#define __NR_write 1\n")
        self.assertEqual(syscall_header(tbl, ("x32",), "__NR_x32_"), "#define __NR_x32_rt_sigaction 512\n")
        auto = autoconf_text({"A": ("bool", "y"), "B": ("tristate", "m"), "C": ("bool", "n"), "S": ("string", 'a"b'),
                              "H": ("hex", "ff"), "I": ("int", "3")})
        for line in ("#define CONFIG_A 1", "#define CONFIG_B_MODULE 1", '#define CONFIG_S "a\\"b"', "#define CONFIG_H 0xff",
                     "#define CONFIG_I 3"):
            self.assertIn(line, auto)
        self.assertNotIn("CONFIG_C", auto)

    def test_default_defconfig_and_tree_files_win(self) -> None:
        files = {"arch/x86/Makefile", "arch/x86/configs/x86_64_defconfig", "arch/x86/configs/i386_defconfig"}
        mk = "ifeq ($(ARCH),x86_64)\n        KBUILD_DEFCONFIG := x86_64_defconfig\nelse\n        KBUILD_DEFCONFIG := i386_defconfig\nendif\n"
        read = {"arch/x86/Makefile": mk}.get
        self.assertEqual(default_defconfig(files, read, "x86_64", "x86"), "arch/x86/configs/x86_64_defconfig")
        self.assertEqual(default_defconfig(files, read, "i386", "x86"), "arch/x86/configs/i386_defconfig")
        out = generated_headers({"Makefile", "include/linux/version.h"}, {"Makefile": "VERSION = 3\nPATCHLEVEL = 0\n"}.get,
                                "x86_64", "x86", {})
        self.assertNotIn("include/linux/version.h", out)  # a tree file is never replaced
        self.assertIn("#define LINUX_VERSION_CODE 196608", out["include/generated/uapi/linux/version.h"])
        self.assertIn("REQUIRED_MASK_BIT_SET", out["arch/x86/include/generated/asm/cpufeaturemasks.h"])


if __name__ == "__main__":
    unittest.main()
