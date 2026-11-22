"""tests/test_compile_env.py - The libclang compilation of a file (parser/compile_env.py) and the headers a
build generates (parser/generated_headers.py)."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import unittest.mock

import clang.cindex as cc

from parser import compile_env
from parser.compile_env import KEEP_GOING, CompileEnv
from parser.generated_headers import (
    autoconf_text,
    bounds_aliases,
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


# An ARCH libclang has no triple for (arch/tile, 2.6.36): 32-bit TILEPro and 64-bit TILE-Gx, chosen by 64BIT
UNMAPPED_TREE = {
    "Makefile": (
        "VERSION = 2\nPATCHLEVEL = 6\nSUBLEVEL = 36\nEXTRAVERSION = -rc1\n"
        "ARCH ?= x86_64\nLINUXINCLUDE := -Iinclude\nCPPFLAGS := -D__KERNEL__ $(LINUXINCLUDE)\n"
        "include arch/$(ARCH)/Makefile\n"
    ),
    "scripts/Makefile.lib": "c_flags = $(CPPFLAGS)\n",
    "arch/x86_64/Makefile": "core-y += arch/x86_64/kernel/\n",
    "arch/x86_64/Kconfig": "config X86_64\n\tbool\n\tdefault y\n",
    "arch/tile/Makefile": "core-y += arch/tile/kernel/\n",
    "arch/tile/Kconfig": "config 64BIT\n\tbool\n\tdefault n\n",
    # CPU_MASK_ALL of include/linux/cpumask.h for NR_CPUS > BITS_PER_LONG, as INIT_TASK() expands it: with a
    # 64-bit long, BITS_TO_LONGS(64) - 2 is (size_t)-1, a range designator libclang expands without end
    "arch/tile/kernel/init_task.c": (
        "#define NR_CPUS 64\n#define BITS_PER_LONG 32\n"
        "#define BITS_TO_LONGS(nr) (((nr) + (8 * sizeof(long)) - 1) / (8 * sizeof(long)))\n"
        "typedef struct { unsigned long bits[BITS_TO_LONGS(NR_CPUS)]; } cpumask_t;\n"
        "struct task_struct { cpumask_t cpus_allowed; };\n"
        "struct task_struct init_task = { .cpus_allowed = (cpumask_t) { {\n"
        "\t[0 ... BITS_TO_LONGS(NR_CPUS) - 2] = ~0UL,\n"
        "\t[BITS_TO_LONGS(NR_CPUS) - 1] = ((NR_CPUS % BITS_PER_LONG) ? (1UL << (NR_CPUS % BITS_PER_LONG)) - 1 : ~0UL)\n"
        "} } };\n"
        "typedef char long_matches_config[sizeof(long) * 8 == BITS_PER_LONG ? 1 : -1];\n"
    ),
}


def _parse_errors_in_child(path: str, args: list[str], options: int, queue) -> None:
    tu = cc.Index.create().parse(path, args=["-w", *args], options=options)
    queue.put(sum(1 for d in tu.diagnostics if d.severity >= cc.Diagnostic.Error))


class TestUnmappedArchTarget(unittest.TestCase):
    """An ARCH without a `_TARGETS` triple: a 32-bit configuration gets a 32-bit target, a 64-bit one the host's."""

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
        env = CompileEnv(root, set(tree), tree.get, None, key=f"test-{id(tree)}", build_base=base)
        self.addCleanup(env.cleanup)
        return env

    def test_32_bit_configuration_parses_with_a_32_bit_long(self) -> None:
        import multiprocessing

        env = self.env_of(UNMAPPED_TREE)
        path = "arch/tile/kernel/init_task.c"
        args, options, arch_env = env.args_for(path)
        self.assertEqual(arch_env.arch, "tile")
        self.assertFalse(arch_env.is_64bit())
        self.assertIn("--target=i386-linux-gnu", args)
        # Bounded: the parse with a 64-bit long never ends
        ctx = multiprocessing.get_context("fork")
        queue = ctx.Queue()
        child = ctx.Process(target=_parse_errors_in_child, args=(os.path.join(env.root, path), args, options, queue))
        child.start()
        child.join(30)
        if child.is_alive():
            child.kill()
            child.join()
            self.fail("libclang did not finish parsing the CPU_MASK_ALL initializer within 30s")
        self.assertEqual(queue.get(timeout=5), 0)

    def test_64_bit_configuration_keeps_the_host_target(self) -> None:
        tree = dict(UNMAPPED_TREE)
        tree["arch/tile/Kconfig"] = "config 64BIT\n\tbool\n\tdefault y\n"
        args, _, arch_env = self.env_of(tree).args_for("arch/tile/kernel/init_task.c")
        self.assertTrue(arch_env.is_64bit())
        self.assertFalse([a for a in args if a.startswith("--target=")])


def _byte_order_source(bits: int, big: bool) -> str:
    order = "__ORDER_BIG_ENDIAN__" if big else "__ORDER_LITTLE_ENDIAN__"
    return (
        f"typedef char long_matches_config[sizeof(long) * 8 == {bits} ? 1 : -1];\n"
        f"#if __BYTE_ORDER__ != {order}\n#error wrong byte order\n#endif\n"
    )


class TestUnmappedArchByteOrder(unittest.TestCase):
    """A big-endian ARCH without a `_TARGETS` triple gets a big-endian triple of its `long` width (configuration
    symbols first, then `_BIG_ENDIAN_ARCHES`); a little-endian one keeps `_GENERIC_TARGETS`."""

    env_of = TestUnmappedArchTarget.env_of

    def args_of(self, arch: str, kconfig: str, bits: int, big: bool) -> tuple[list[str], int, object]:
        tree = dict(UNMAPPED_TREE)
        tree[f"arch/{arch}/Makefile"] = f"core-y += arch/{arch}/kernel/\n"
        tree[f"arch/{arch}/Kconfig"] = kconfig
        path = f"arch/{arch}/kernel/order.c"
        tree[path] = _byte_order_source(bits, big)
        env = self.env_of(tree)
        args, options, arch_env = env.args_for(path)
        self.assertEqual(arch_env.arch, arch)
        tu = cc.Index.create().parse(os.path.join(env.root, path), args=["-w", *args], options=options)
        errors = [d.spelling for d in tu.diagnostics if d.severity >= cc.Diagnostic.Error]
        self.assertEqual(errors, [], args)
        return args, options, arch_env

    def test_inherently_big_endian_32_bit_arch(self) -> None:
        args, _, arch_env = self.args_of("parisc", "config 64BIT\n\tbool\n\tdefault n\n", 32, True)
        self.assertTrue(arch_env.is_big_endian())
        self.assertIn("--target=powerpc-linux-gnu", args)

    def test_inherently_big_endian_64_bit_arch(self) -> None:
        args, _, arch_env = self.args_of("parisc", "config 64BIT\n\tbool\n\tdefault y\n", 64, True)
        self.assertTrue(arch_env.is_64bit())
        self.assertIn("--target=powerpc64-linux-gnu", args)

    def test_cpu_big_endian_symbol_selects_big_endian(self) -> None:
        kconfig = "config 64BIT\n\tbool\n\tdefault n\n\nconfig CPU_BIG_ENDIAN\n\tbool\n\tdefault y\n"
        args, _, _ = self.args_of("sh", kconfig, 32, True)
        self.assertIn("--target=powerpc-linux-gnu", args)
        kconfig64 = "config 64BIT\n\tbool\n\tdefault y\n\nconfig CPU_BIG_ENDIAN\n\tbool\n\tdefault y\n"
        args, _, _ = self.args_of("tile", kconfig64, 64, True)
        self.assertIn("--target=powerpc64-linux-gnu", args)

    def test_little_endian_symbols_keep_the_generic_targets(self) -> None:
        # sh: CPU_BIG_ENDIAN not set; m32r (big-endian by default): CPU_LITTLE_ENDIAN=y
        kconfig = "config 64BIT\n\tbool\n\tdefault n\n\nconfig CPU_BIG_ENDIAN\n\tbool \"big\"\n\tdefault n\n"
        args, _, arch_env = self.args_of("sh", kconfig, 32, False)
        self.assertFalse(arch_env.is_big_endian())
        self.assertIn("--target=i386-linux-gnu", args)
        kconfig = "config 64BIT\n\tbool\n\tdefault n\n\nconfig CPU_LITTLE_ENDIAN\n\tbool \"le\"\n\tdefault y\n"
        args, _, _ = self.args_of("m32r", kconfig, 32, False)
        self.assertIn("--target=i386-linux-gnu", args)
        args, _, _ = self.args_of("m32r", "config 64BIT\n\tbool\n\tdefault n\n", 32, True)
        self.assertIn("--target=powerpc-linux-gnu", args)

    def test_m68k_takes_its_own_triple_when_libclang_has_it(self) -> None:
        kconfig = "config 64BIT\n\tbool\n\tdefault n\n"
        if compile_env.target_supported("m68k-linux-gnu"):
            args, _, _ = self.args_of("m68k", kconfig, 32, True)
            self.assertIn("--target=m68k-linux-gnu", args)
        with unittest.mock.patch.object(compile_env, "target_supported", return_value=False):
            args, _, _ = self.args_of("m68k", kconfig, 32, True)
        self.assertIn("--target=powerpc-linux-gnu", args)

    def test_mapped_arches_keep_their_targets(self) -> None:
        self.assertEqual(compile_env._TARGETS["mips"], ("mips64-linux-gnu", "mips-linux-gnu"))
        self.assertEqual(compile_env._GENERIC_TARGETS, (None, "i386-linux-gnu"))
        self.assertFalse(set(compile_env._BIG_ENDIAN_ARCHES) & set(compile_env._TARGETS))


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


class TestHeaderPrelude(unittest.TestCase):
    """compile_env.header_prelude(): the retry prelude of a header that is not self-contained."""

    def setUp(self) -> None:
        self.tree = dict(TREE)
        self.tree["include/linux/types.h"] = "#ifndef _T\n#define _T\n#include <asm/atomic.h>\ntypedef unsigned int u32;\n#endif\n"
        self.tree["include/linux/lockd/bind.h"] = "struct b { u32 cookie; };\n"
        self.root = tempfile.mkdtemp(prefix="kip-env-", dir="/dev/shm" if os.path.isdir("/dev/shm") else None)
        for path, text in self.tree.items():
            dest = os.path.join(self.root, path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="latin-1") as fh:
                fh.write(text)
        self.base = tempfile.mkdtemp(prefix="kip-build-")
        self.env = CompileEnv(self.root, set(self.tree), self.tree.get, None, key="prelude", build_base=self.base)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(shutil.rmtree, self.base, True)
        self.addCleanup(self.env.cleanup)

    def test_prelude_applies_to_headers_outside_its_closure(self) -> None:
        arch_env = self.env.env("x86_64")
        prelude = ["-include", os.path.join(self.root, compile_env.HEADER_PRELUDE)]
        self.assertEqual(self.env.header_prelude("include/linux/lockd/bind.h", arch_env), prelude)
        self.assertEqual(self.env.header_prelude("fs/x.c", arch_env), [])                       # not a header
        self.assertEqual(self.env.header_prelude("include/linux/types.h", arch_env), [])         # the prelude itself
        self.assertEqual(self.env.header_prelude("include/asm-x86_64/atomic.h", arch_env), [])   # read by the prelude
        self.assertEqual(self.env.header_prelude("include/linux/lockd/bind.h", None), [])        # no ARCH build
        # The closure survives the pickling a parse worker receives
        import pickle
        clone = pickle.loads(pickle.dumps(self.env))
        self.assertEqual(clone._prelude_closure, self.env._prelude_closure)
        self.assertEqual(clone.header_prelude("include/linux/lockd/bind.h", arch_env), prelude)

    def test_unknown_type_detection(self) -> None:
        path = "include/linux/lockd/bind.h"
        args, options, arch_env = self.env.args_for(path)
        full = os.path.join(self.root, path)
        index = cc.Index.create()
        plain = index.parse(full, args=["-w", *args], options=options)
        self.assertTrue(compile_env.has_unknown_type(plain, full))
        retry = index.parse(full, args=["-w", *args, *self.env.header_prelude(path, arch_env)], options=options)
        self.assertFalse(compile_env.has_unknown_type(retry, full))
        self.assertLess(compile_env.diagnostics_fact(retry, arch_env)["errors"],
                        compile_env.diagnostics_fact(plain, arch_env)["errors"])


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
        bounds = "DEFINE(NR_PAGEFLAGS, __NR_PAGEFLAGS);\nDEFINE(MAX_NR_ZONES, __MAX_NR_ZONES);\nDEFINE(SPINLOCK_SIZE, sizeof(spinlock_t));\n"
        self.assertEqual(bounds_aliases(bounds), {"NR_PAGEFLAGS": "__NR_PAGEFLAGS", "MAX_NR_ZONES": "__MAX_NR_ZONES"})
        text = generated_headers({"Makefile", "kernel/bounds.c"}, {"Makefile": "VERSION = 3\n", "kernel/bounds.c": bounds}.get,
                                 "x86_64", "x86", {})["include/generated/bounds.h"]
        # An enumerator value reads as 0 in #if and as the enum's value in C; an expression value stays 0
        for line in ("#define MAX_NR_ZONES __MAX_NR_ZONES", "#define NR_PAGEFLAGS __NR_PAGEFLAGS", "#define SPINLOCK_SIZE 0"):
            self.assertIn(line, text)

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
