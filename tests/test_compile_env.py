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

    def test_resolve_include_searches_like_the_build(self) -> None:
        x86 = self.env.env("x86_64")
        i386 = self.env.env("i386")
        # The layout's include/asm link, per ARCH
        self.assertEqual(self.env.resolve_include("asm/atomic.h", "fs/x.c", arch_env=x86), "include/asm-x86_64/atomic.h")
        self.assertEqual(self.env.resolve_include("asm/atomic.h", "arch/i386/kernel/entry.S", arch_env=i386),
                         "include/asm-i386/atomic.h")
        self.assertEqual(self.env.resolve_include("linux/types.h", "fs/x.c", arch_env=x86), "include/linux/types.h")
        # The quoted form searches the including file's directory first
        self.assertEqual(self.env.resolve_include("x.c", "fs/other.c", quoted=True, arch_env=x86), "fs/x.c")
        self.assertIsNone(self.env.resolve_include("x.c", "fs/other.c", arch_env=x86))
        # Build products and host headers are not tree files
        self.assertIsNone(self.env.resolve_include("linux/autoconf.h", "fs/x.c", arch_env=x86))
        self.assertIsNone(self.env.resolve_include("stdio.h", "fs/x.c", arch_env=x86))
        self.assertIsNone(self.env.resolve_include("/abs.h", "fs/x.c", arch_env=x86))
        # Without a layout: the file's directory and include/
        self.assertEqual(self.env.resolve_include("linux/types.h", "fs/x.c"), "include/linux/types.h")
        self.env.cleanup()

    def test_tree_without_makefile(self) -> None:
        bare = tempfile.mkdtemp(prefix="kip-bare-")
        self.addCleanup(shutil.rmtree, bare, True)
        env = CompileEnv(bare, {"a.c"}, lambda p: None, None, build_base=self.base)
        args, options, arch_env = env.args_for("a.c")
        self.assertIsNone(arch_env)
        self.assertIn("-nostdinc", args)
        self.assertIn(f"-I{bare}/include", args)

    def test_per_directory_ccflags_and_subdir_ccflags_includes(self) -> None:
        extra_tree = {
            **TREE,
            "drivers/gpu/drm/Makefile": (
                "subdir-ccflags-y += -I$(srctree)/drivers/gpu/drm/include -I$(src)/shared\n"
                "ccflags-y += -I$(src)/local_only\n"
            ),
            "drivers/gpu/drm/amd/Kbuild": (
                "ccflags-y += -I$(src)/inc -I$(srctree)/$(src)/uapi -I/outside/escape -I$(src)/../../../../etc\n"
            ),
            "fs/xfs/Makefile": "EXTRA_CFLAGS += -I$(TOPDIR)/fs/xfs/libxfs -I$(obj)/scrub\n",
            "drivers/gpu/drm/include/drm_ common.h": "",
            "drivers/gpu/drm/shared/drm_shared.h": "",
            "drivers/gpu/drm/local_only/drm_local.h": "",
            "drivers/gpu/drm/amd/inc/amd_inc.h": "",
            "drivers/gpu/drm/amd/uapi/amd_uapi.h": "",
            "drivers/gpu/drm/amd/amd.c": "",
            "drivers/gpu/drm/drm_drv.c": "",
            "fs/xfs/libxfs/xfs_fs.h": "",
            "fs/xfs/scrub/scrub.h": "",
            "fs/xfs/xfs_super.c": "",
        }
        env = CompileEnv(self.root, set(extra_tree), extra_tree.get, None, key="test-ccflags", build_base=self.base)

        drm_args, _, _ = env.args_for("drivers/gpu/drm/drm_drv.c")
        self.assertIn(f"-I{self.root}/drivers/gpu/drm/include", drm_args)
        self.assertIn(f"-I{self.root}/drivers/gpu/drm/shared", drm_args)
        self.assertIn(f"-I{self.root}/drivers/gpu/drm/local_only", drm_args)

        # Subdirectory inherits subdir-ccflags-y from drivers/gpu/drm/Makefile, plus its own Kbuild ccflags-y,
        # but NOT drivers/gpu/drm's local-only ccflags-y or outside-repo paths
        amd_args, _, _ = env.args_for("drivers/gpu/drm/amd/amd.c")
        self.assertIn(f"-I{self.root}/drivers/gpu/drm/include", amd_args)
        self.assertIn(f"-I{self.root}/drivers/gpu/drm/shared", amd_args)
        self.assertIn(f"-I{self.root}/drivers/gpu/drm/amd/inc", amd_args)
        self.assertIn(f"-I{self.root}/drivers/gpu/drm/amd/uapi", amd_args)
        self.assertNotIn(f"-I{self.root}/drivers/gpu/drm/local_only", amd_args)
        self.assertFalse(any("outside" in a or "etc" in a for a in amd_args))

        # EXTRA_CFLAGS with $(TOPDIR) and $(obj)
        xfs_args, _, _ = env.args_for("fs/xfs/xfs_super.c")
        self.assertIn(f"-I{self.root}/fs/xfs/libxfs", xfs_args)
        self.assertIn(f"-I{self.root}/fs/xfs/scrub", xfs_args)

    def test_cleanup_lifecycle_and_owner_tracking(self) -> None:
        from core.FileHandler import OWNER_FILE, owner_alive
        from core.ingest_state import cleanup_ramdisk_artifacts

        arch_env = self.env.env("x86_64")
        self.assertIsNotNone(arch_env)
        self.assertTrue(os.path.isfile(os.path.join(arch_env.build_dir, OWNER_FILE)))
        self.assertTrue(owner_alive(arch_env.build_dir))

        # Simulated crashed worker build directory inside kip_build with dead PID is cleaned by cleanup_ramdisk_artifacts
        kip_root = os.path.join(self.base, "kip_build")
        stale_dir = os.path.join(kip_root, "deadbeef-arm64")
        live_dir = os.path.join(kip_root, "cafebabe-x86_64")
        os.makedirs(stale_dir, exist_ok=True)
        os.makedirs(live_dir, exist_ok=True)
        with open(os.path.join(stale_dir, OWNER_FILE), "w") as fh:
            fh.write("999999999")
        with open(os.path.join(live_dir, OWNER_FILE), "w") as fh:
            fh.write(str(os.getpid()))

        removed = cleanup_ramdisk_artifacts(self.base, search_system_temps=False)
        self.assertGreaterEqual(removed, 1)
        self.assertFalse(os.path.exists(stale_dir))
        self.assertTrue(os.path.exists(live_dir))


# A 2.3.35-style tree: `TOPDIR := $(shell ... pwd ...)`, `CPPFLAGS := -D__KERNEL__ -I$(HPATH)`, include/asm from
# the `symlinks:` rule
PRE_2_6_TREE = {
    "Makefile": (
        "ARCH := $(shell uname -m | sed -e s/i.86/i386/)\n"
        'TOPDIR := $(shell if [ "$$PWD" != "" ]; then echo $$PWD; else pwd; fi)\n'
        "HPATH = $(TOPDIR)/include\nCPPFLAGS := -D__KERNEL__ -I$(HPATH)\nCFLAGS := $(CPPFLAGS) -O2\n"
        "include arch/$(ARCH)/Makefile\n"
        "symlinks:\n\trm -f include/asm\n\t( cd include ; ln -sf asm-$(ARCH) asm)\n"
    ),
    "arch/i386/Makefile": "CFLAGS := $(CFLAGS) -pipe\n",
    "include/asm-i386/types.h": "typedef unsigned int __u32;\n",
    "include/linux/lockd/bind.h": "#include <linux/lockd/nlm.h>\n#include <asm/types.h>\n__u32 nlm_value = NLM_PROGRAM;\n",
    "include/linux/lockd/nlm.h": "#define NLM_PROGRAM 100021\n",
}

# A 1.0-style tree: no arch/, include/asm/ a directory of the tree, no -I flag
PRE_ARCH_TREE = {
    "Makefile": "CFLAGS = -Wall -O2\nCC = gcc -D__KERNEL__\nCPP = $(CC) -E\n",
    "kernel/sched.c": "#include <linux/sched.h>\n#include <asm/system.h>\nint x = SCHED_X + SYSTEM_X;\n",
    "include/linux/sched.h": "#define SCHED_X 1\n", "include/asm/system.h": "#define SYSTEM_X 2\n",
}


class TestHistoricalCompileEnv(unittest.TestCase):
    """CompileEnv of trees before kbuild's c_flags (2.x) and before arch/ (0.01-1.0)."""

    def env_of(self, tree: dict[str, str]) -> CompileEnv:
        root = tempfile.mkdtemp(prefix="kip-env-", dir="/dev/shm" if os.path.isdir("/dev/shm") else None)
        for path, text in tree.items():
            dest = os.path.join(root, path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="latin-1") as fh:
                fh.write(text)
        base = tempfile.mkdtemp(prefix="kip-build-")
        self.addCleanup(shutil.rmtree, root, True)
        self.addCleanup(shutil.rmtree, base, True)
        env = CompileEnv(root, set(tree), tree.get, None, key=f"test-{id(tree)}", build_base=base, configure=False)
        self.addCleanup(env.cleanup)
        return env

    def parsed_includes(self, env: CompileEnv, path: str) -> tuple[set[str], int]:
        args, options, _ = env.args_for(path)
        tu = cc.Index.create().parse(os.path.join(env.root, path), args=args, options=options)
        errors = sum(1 for d in tu.diagnostics if d.severity >= cc.Diagnostic.Error)
        return {env.repo_path(i.include.name) for i in tu.get_includes()}, errors

    def test_pre_2_6_include_flags_and_symlinks(self) -> None:
        env = self.env_of(PRE_2_6_TREE)
        self.assertEqual(env.default_arch, "i386")
        i386 = env.env("i386")
        self.assertEqual((i386.layout.includes, i386.layout.links), (["include/"], {"include/asm/": "include/asm-i386/"}))
        bind = "include/linux/lockd/bind.h"
        self.assertEqual(env.resolve_include("linux/lockd/nlm.h", bind, arch_env=i386), "include/linux/lockd/nlm.h")
        self.assertEqual(env.resolve_include("asm/types.h", bind, arch_env=i386), "include/asm-i386/types.h")
        self.assertIn(f"-I{env.root}/include", env.args_for(bind)[0])
        includes, errors = self.parsed_includes(env, bind)
        self.assertEqual((includes, errors), ({"include/linux/lockd/nlm.h", "include/asm-i386/types.h"}, 0))

    def test_tree_before_arch(self) -> None:
        env = self.env_of(PRE_ARCH_TREE)
        self.assertEqual((env.arch_values, env.default_arch), (["i386"], "i386"))
        i386 = env.env("i386")
        self.assertEqual((i386.layout.sources, i386.layout.includes), ([], ["include/"]))
        self.assertEqual(env.resolve_include("asm/system.h", "kernel/sched.c", arch_env=i386), "include/asm/system.h")
        includes, errors = self.parsed_includes(env, "kernel/sched.c")
        self.assertEqual((includes, errors), ({"include/linux/sched.h", "include/asm/system.h"}, 0))


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

    def test_changeset_and_compile_env_pickle_cleanly(self):
        """Worker IPC pickling succeeds after C/ASM parse and CompileEnv itself is picklable."""
        import pickle
        from core.TableHandling import ChangeSet
        from pipeline.compile_envs import _reader

        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as build:
            with open(os.path.join(root, "Makefile"), "w") as fh:
                fh.write("VERSION = 0\nPATCHLEVEL = 1\n")
            with open(os.path.join(root, "main.c"), "w") as fh:
                fh.write("int main(void) { return 0; }\n")
            env = CompileEnv(root, {"Makefile", "main.c"}, _reader(root), build_base=build)
            try:
                # Even if CompileEnv itself is pickled, TreeFileReader and __getstate__ must be picklable
                roundtripped_env = pickle.loads(pickle.dumps(env))
                self.assertEqual(roundtripped_env.read("Makefile"), "VERSION = 0\nPATCHLEVEL = 1\n")

                cs = ChangeSet("A\tmain.c")
                cs.compile_env = env
                cs.compile_arch = None
                cs.parse_diagnostics = {"arch": None}
                cs.clear_bloat()
                self.assertIsNone(getattr(cs, "compile_env", None))
                self.assertIsNone(getattr(cs, "compile_arch", None))
                self.assertIsNone(getattr(cs, "parse_diagnostics", None))
                pickle.loads(pickle.dumps(cs))
            finally:
                env.cleanup()


if __name__ == "__main__":
    unittest.main()
