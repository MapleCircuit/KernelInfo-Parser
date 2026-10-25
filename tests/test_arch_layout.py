"""tests/test_arch_layout.py - Make evaluation (parser/make_eval.py) and build layouts (parser/arch_layout.py)."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from parser.arch_layout import (
    ROLE_INCLUDE,
    ROLE_LINK,
    ArchLayout,
    compute_layouts,
    path_arch,
)
from parser.make_eval import SRC_ROOT, UNKNOWN, MakeEvaluator, Tree, tree_path


def evaluator(files: dict[str, str], **cmdline: str) -> MakeEvaluator:
    ev = MakeEvaluator(Tree(files), files.get, cmdline)
    ev.include("Makefile")
    return ev


class TestMakeEvaluator(unittest.TestCase):
    def test_assignment_flavors_and_command_line_precedence(self) -> None:
        ev = evaluator({"Makefile": (
            "A = $(B)\nB = late\nC := $(B)\nB = later\nD ?= one\nD ?= two\nE := x\nE += y\n"
            "ARCH := ignored\noverride F := over\nG = 1\nG += $(B)\n"
        )}, ARCH="x86_64", F="cmd")
        self.assertEqual(ev.value("A"), "later")  # recursive: expanded at use
        self.assertEqual(ev.value("C"), "late")   # simple: expanded at assignment
        self.assertEqual((ev.value("D"), ev.value("E"), ev.value("G")), ("one", "x y", "1 later"))
        self.assertEqual(ev.value("ARCH"), "x86_64")  # command line wins...
        self.assertEqual(ev.value("F"), "over")       # ...unless override

    def test_conditionals_known_unknown_and_else_if(self) -> None:
        ev = evaluator({"Makefile": (
            "ifeq ($(ARCH),i386)\n  SRCARCH := x86\nelse ifeq ($(ARCH),x86_64)\n  SRCARCH := x86\nelse\n  SRCARCH := $(ARCH)\nendif\n"
            "ifdef CONFIG_SMP\n  SMP := y\nendif\n"
            "CCV := $(shell $(CC) --version)\n"
            "ifeq ($(CCV),gcc)\n  T := a\nelse\n  T := b\nendif\n"
            "ifneq ($(SRCARCH),x86)\n  NOTX86 := 1\nendif\n"
        )}, ARCH="x86_64")
        self.assertEqual(ev.value("SRCARCH"), "x86")
        self.assertEqual(ev.value("SMP"), "")  # CONFIG_* are undefined (no .config)
        self.assertEqual(ev.value("T"), "b")   # unknown condition: both branches apply, in order
        self.assertFalse(ev.defined("NOTX86"))

    def test_functions(self) -> None:
        files = {"Makefile": (
            "srctree := $(CURDIR)\ndirs := a b\n"
            "P := $(patsubst %,-I$(srctree)/%/include,$(dirs))\n"
            "F := $(filter %.h,x.h y.c z.h)\nFO := $(filter-out x.h,x.h z.h)\n"
            "FE := $(foreach d,$(dirs),$(d)-y)\n"
            "fn = <$(1)|$(2)>\nC := $(call fn,one,two)\n"
            "W := $(wildcard $(srctree)/inc/*.h) $(wildcard $(srctree)/inc/missing.h)\n"
            "I := $(if $(KBUILD_SRC),-Iout,-Iin)\n"
            "S := $(subst x86_64,x86,$(ARCH))\nSUB := $(ARCH:%_64=%)\n"
            "O := $(origin ARCH) $(origin V) $(origin dirs)\n"
            "X := $(file <foo)\n"
        ), "inc/a.h": "", "inc/b.h": "", "inc/c.c": ""}
        ev = evaluator(files, ARCH="x86_64")
        self.assertEqual(ev.value("P"), f"-I{SRC_ROOT}/a/include -I{SRC_ROOT}/b/include")
        self.assertEqual((ev.value("F"), ev.value("FO"), ev.value("FE")), ("x.h z.h", "z.h", "a-y b-y"))
        self.assertEqual(ev.value("C"), "<one|two>")
        self.assertEqual(ev.value("W").split(), [f"{SRC_ROOT}/inc/a.h", f"{SRC_ROOT}/inc/b.h"])
        self.assertEqual((ev.value("I"), ev.value("S"), ev.value("SUB")), ("-Iin", "x86", "x86"))
        self.assertEqual(ev.value("O"), "command line undefined file")
        self.assertIn(UNKNOWN, ev.value("X"))

    def test_shell_uname_is_the_host_machine_through_sed(self) -> None:
        ev = evaluator({"Makefile": "SUBARCH := $(shell uname -m | sed -e s/i.86/x86/ -e s/x86_64/x86/ -e s/arm.*/arm/)\n"
                                    "RAW := $(shell uname -m)\nOTHER := $(shell date)\n"})
        self.assertEqual((ev.value("SUBARCH"), ev.value("RAW")), ("x86", "x86_64"))
        self.assertEqual(ev.value("OTHER"), UNKNOWN)

    def test_includes_defines_and_recipes(self) -> None:
        files = {
            "Makefile": (
                "srctree := $(CURDIR)\ninclude $(srctree)/scripts/defs.mk\n-include include/config/auto.conf\n"
                "include/asm: FORCE\n\t$(Q)$(make-link)\n"
                "ifeq ($(ARCH),x86_64)\n\t@echo only-x86_64 >$@\nendif\n"
            ),
            "scripts/defs.mk": "Q := @\ndefine make-link\n\tln -fsn asm-$(ARCH) $@\nendef\n",
        }
        ev = evaluator(files, ARCH="x86_64")
        (rule,) = ev.rules_for("include/asm")
        self.assertEqual([c.strip() for c in ev.recipe_commands(rule, "include/asm")],
                         ["@\tln -fsn asm-x86_64 include/asm", "@echo only-x86_64 >include/asm"])
        self.assertEqual(ev.value("MAKEFILE_LIST"), "Makefile scripts/defs.mk")

    def test_tree_paths(self) -> None:
        self.assertEqual(tree_path(f"{SRC_ROOT}/arch/x86/include"), "arch/x86/include")
        self.assertEqual(tree_path("./include"), "include")
        self.assertEqual(tree_path("../asm-x86_64", "include/asm-um"), "include/asm-x86_64")
        self.assertIsNone(tree_path("/usr/include"))
        self.assertIsNone(tree_path("a" + UNKNOWN))


# A 2.6.12-style tree: include/asm is a symlink made by the top Makefile; ARCH=um links its arch headers
# to the host's (SUBARCH from `uname -m`) from a recipe that changes directory first.
OLD_TREE = {
    "Makefile": (
        "SUBARCH := $(shell uname -m | sed -e s/i.86/i386/ -e s/sun4u/sparc64/)\n"
        "ARCH ?= $(SUBARCH)\n"
        "srctree := $(if $(KBUILD_SRC),$(KBUILD_SRC),$(CURDIR))\nTOPDIR := $(srctree)\n"
        "LINUXINCLUDE := -Iinclude $(if $(KBUILD_SRC),-Iinclude2 -I$(srctree)/include)\n"
        "CPPFLAGS := -D__KERNEL__ $(LINUXINCLUDE)\n"
        "include $(srctree)/arch/$(ARCH)/Makefile\n"
        "include/asm:\n\t@echo '  SYMLINK $@ -> include/asm-$(ARCH)'\n"
        "\t$(Q)if [ ! -d include ]; then mkdir -p include; fi;\n\t@ln -fsn asm-$(ARCH) $@\n"
    ),
    "scripts/Makefile.lib": "c_flags = -Wp,-MD,$(depfile) $(NOSTDINC_FLAGS) $(CPPFLAGS) $(CFLAGS)\n",
    "arch/x86_64/Makefile": "core-y += arch/x86_64/kernel/\n",
    "arch/x86_64/kernel/setup.c": "",
    "arch/i386/Makefile": "core-y += arch/i386/kernel/\n",
    "arch/um/Makefile": (
        "ARCH_DIR := arch/um\nCFLAGS += -I$(srctree)/$(ARCH_DIR)/include\n"
        "prepare: include/asm-um/arch\n"
        "include/asm-um/arch:\n\t@echo '  SYMLINK $@'\n"
        "ifneq ($(KBUILD_SRC),)\n\t$(Q)ln -fsn $(srctree)/include/asm-$(SUBARCH) include/asm-um/arch\n"
        "else\n\t$(Q)cd $(TOPDIR)/include/asm-um && ln -sf ../asm-$(SUBARCH) arch\nendif\n"
    ),
    "arch/um/include/user.h": "",
    "include/asm-x86_64/atomic.h": "", "include/asm-i386/atomic.h": "", "include/asm-um/atomic.h": "",
    "include/linux/sched.h": "",
}

# A 5.x-style tree: SRCARCH aliases, hdr-arch, generated include directories, forced includes and
# scripts/Makefile.asm-generic wrappers (generic-y plus the mandatory-y fallback).
NEW_TREE = {
    "Makefile": (
        "this-makefile := $(lastword $(MAKEFILE_LIST))\nabs_objtree := $(CURDIR)\n"
        "abs_srctree := $(realpath $(dir $(this-makefile)))\n"
        "ifeq ($(abs_srctree),$(abs_objtree))\n        srctree := .\nelse\n        srctree := $(abs_srctree)\nendif\n"
        "objtree := .\nexport srctree objtree\n"
        "SRCARCH := $(ARCH)\nifeq ($(ARCH),x86_64)\n        SRCARCH := x86\nendif\n"
        "ifeq ($(ARCH),i386)\n        SRCARCH := x86\nendif\n"
        "hdr-arch := $(SRCARCH)\nifeq ($(ARCH),m68knommu)\n       hdr-arch := m68k\nendif\nexport SRCARCH hdr-arch\n"
        "USERINCLUDE := -I$(srctree)/arch/$(hdr-arch)/include/uapi -I$(srctree)/include/uapi "
        "-include $(srctree)/include/linux/kconfig.h\n"
        "LINUXINCLUDE := -I$(srctree)/arch/$(hdr-arch)/include -I$(objtree)/arch/$(hdr-arch)/include/generated "
        "$(if $(building_out_of_srctree),-I$(srctree)/include) -I$(objtree)/include $(USERINCLUDE)\n"
        "include $(srctree)/arch/$(SRCARCH)/Makefile\n"
        "asm-generic := -f $(srctree)/scripts/Makefile.asm-generic obj\n"
        "asm-generic:\n\t$(Q)$(MAKE) $(asm-generic)=arch/$(SRCARCH)/include/generated/asm generic=include/asm-generic\n"
    ),
    "scripts/Makefile.lib": "c_flags = $(LINUXINCLUDE) -include $(srctree)/include/linux/compiler_types.h $(KBUILD_CPPFLAGS)\n",
    "scripts/Makefile.asm-generic": (
        "all:\nsrc := $(subst /generated,,$(obj))\n-include $(src)/Kbuild\n"
        "ifneq ($(SRCARCH),um)\ninclude $(srctree)/$(generic)/Kbuild\nendif\n"
        "mandatory-y := $(filter-out $(generated-y), $(mandatory-y))\n"
        "generic-y += $(foreach f, $(mandatory-y), $(if $(wildcard $(srctree)/$(src)/$(f)),,$(f)))\n"
        "generic-y := $(addprefix $(obj)/, $(generic-y))\nall: $(generic-y)\n\t@:\n"
    ),
    "include/asm-generic/Kbuild": "mandatory-y += atomic.h\nmandatory-y += bug.h\n",
    "include/asm-generic/atomic.h": "", "include/asm-generic/bug.h": "", "include/asm-generic/early_ioremap.h": "",
    "include/linux/kconfig.h": "", "include/linux/compiler_types.h": "", "include/uapi/linux/types.h": "",
    "arch/x86/Makefile": "KBUILD_CPPFLAGS += -I$(srctree)/arch/x86/include/asm/mach-default\n",
    "arch/x86/include/asm/Kbuild": "generic-y += early_ioremap.h\n",
    "arch/x86/include/asm/atomic.h": "", "arch/x86/include/asm/mach-default/irq.h": "",
    "arch/x86/include/uapi/asm/types.h": "",
    "arch/m68knommu/Makefile": "", "arch/m68k/include/asm/atomic.h": "", "arch/m68k/Makefile": "",
}


class TestArchLayout(unittest.TestCase):
    def test_2_6_symlinked_asm_headers(self) -> None:
        layouts = compute_layouts(OLD_TREE, OLD_TREE.get)
        self.assertEqual(sorted(layouts), ["i386", "um", "x86_64"])
        x86_64 = layouts["x86_64"]
        self.assertEqual(x86_64.srcarch, "x86_64")
        self.assertEqual(x86_64.includes, ["include/"])
        self.assertEqual(x86_64.links, {"include/asm/": "include/asm-x86_64/"})
        self.assertEqual(x86_64.sources, ["arch/x86_64/"])
        self.assertEqual(x86_64.arch_trees(), {"x86_64"})
        # ARCH=um: its own include directory, asm -> asm-um, asm-um/arch -> the host's asm-x86_64
        um = layouts["um"]
        self.assertEqual(um.includes, ["include/", "arch/um/include/"])
        self.assertEqual(um.links, {"include/asm/": "include/asm-um/", "include/asm-um/arch/": "include/asm-x86_64/"})
        self.assertEqual(um.arch_trees(), {"um", "x86_64"})
        self.assertEqual(compute_layouts(OLD_TREE, OLD_TREE.get, host_machine="i686")["um"].arch_trees(), {"um", "i386"})

    def test_include_paths_wrappers_and_forced_includes(self) -> None:
        layouts = compute_layouts(NEW_TREE, NEW_TREE.get)
        self.assertEqual(sorted(layouts), ["i386", "m68k", "m68knommu", "x86", "x86_64"])
        x86_64 = layouts["x86_64"]
        self.assertEqual(x86_64.srcarch, "x86")
        self.assertEqual(x86_64.includes, [
            "arch/x86/include/", "arch/x86/include/generated/", "include/", "arch/x86/include/uapi/",
            "include/uapi/", "arch/x86/include/asm/mach-default/",
        ])
        self.assertEqual(x86_64.forced, ["include/linux/kconfig.h", "include/linux/compiler_types.h"])
        # generic-y, plus mandatory-y headers the architecture does not provide (atomic.h it does)
        self.assertEqual(x86_64.wrappers, {
            "arch/x86/include/generated/asm/early_ioremap.h": "include/asm-generic/early_ioremap.h",
            "arch/x86/include/generated/asm/bug.h": "include/asm-generic/bug.h",
        })
        self.assertEqual(x86_64.generic_headers(), {"include/asm-generic/early_ioremap.h", "include/asm-generic/bug.h"})
        # hdr-arch: m68knommu's headers come from arch/m68k/
        nommu = layouts["m68knommu"]
        self.assertEqual(nommu.includes[0], "arch/m68k/include/")
        self.assertEqual(nommu.arch_trees(), {"m68knommu", "m68k"})

    def test_rows_round_trip(self) -> None:
        layout = compute_layouts(NEW_TREE, NEW_TREE.get, ["x86_64"])["x86_64"]
        rows = layout.rows()
        self.assertEqual([r.path for r in rows if r.role == ROLE_INCLUDE][:2], ["arch/x86/include/", "arch/x86/include/generated/"])
        again = ArchLayout.from_rows("x86_64", [(r.role, r.priority, r.path, r.target) for r in reversed(rows)])
        self.assertEqual((again.srcarch, again.includes, again.wrappers, again.forced, again.sources),
                         (layout.srcarch, layout.includes, layout.wrappers, layout.forced, layout.sources))
        self.assertFalse([r for r in rows if r.role == ROLE_LINK])

    def test_path_arch(self) -> None:
        self.assertEqual(path_arch("arch/x86/kernel/setup.c"), "x86")
        self.assertEqual(path_arch("include/asm-x86_64/atomic.h"), "x86_64")
        self.assertIsNone(path_arch("include/asm-generic/atomic.h"))
        self.assertIsNone(path_arch("arch/Kconfig"))


class TestStoredLayouts(unittest.TestCase):
    def test_step_6_8_replaces_the_version_rows(self) -> None:
        """main.processing_arch_layouts(): rows of every ARCH for the head commit, previous rows replaced."""
        from contextlib import contextmanager
        from unittest.mock import MagicMock, patch

        import main

        log: list = []
        cursor = MagicMock()
        cursor.execute.side_effect = lambda sql, params=(): log.append((sql.split()[0], params))
        cursor.executemany.side_effect = lambda sql, rows: log.append(("INSERT", list(rows)))
        db = MagicMock()
        db.cnx.cursor.return_value = cursor

        @contextmanager
        def fake_db():
            yield db

        with patch.object(main.gp, "VID", 7), patch.object(main.gp, "Version_Name", "v2.6.12"), \
             patch.object(main.gp, "file_blob_hashes", dict.fromkeys(OLD_TREE, b"\0" * 20), create=True), \
             patch.object(main.MF, "get_file", side_effect=lambda path, version: OLD_TREE[path]), \
             patch.object(main.G, "DB", fake_db):
            stored = main.processing_arch_layouts("v2.6.12")
        self.assertEqual(log[0], ("DELETE", (7,)))
        rows = log[1][1]
        self.assertEqual(stored, len(rows))
        self.assertIn((7, "x86_64", ROLE_LINK, 0, "include/asm/", "include/asm-x86_64/"), rows)
        self.assertEqual({r[1] for r in rows}, {"i386", "um", "x86_64"})
        db.cnx.commit.assert_called_once()


REPO = Path(__file__).resolve().parent.parent / "repos" / "linux"


@unittest.skipUnless((REPO / ".git").exists() or (REPO / "HEAD").exists(), "Linux repository not available")
class TestRealKernelLayouts(unittest.TestCase):
    """The layout of real releases, evaluated from their Makefiles."""

    @staticmethod
    def layout(version: str, arch: str) -> ArchLayout | None:
        def git(*args: str) -> str:
            return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, errors="replace", check=False).stdout

        files = git("ls-tree", "-r", "--name-only", version).splitlines()
        if not files:
            raise unittest.SkipTest(f"{version} not in the repository")
        return compute_layouts(files, lambda p: git("show", f"{version}:{p}"), [arch]).get(arch)

    def test_v2_6_12_x86_64(self) -> None:
        layout = self.layout("v2.6.12", "x86_64")
        self.assertEqual(layout.links["include/asm/"], "include/asm-x86_64/")
        self.assertEqual(layout.arch_trees(), {"x86_64"})

    def test_v3_0_x86_64(self) -> None:
        layout = self.layout("v3.0", "x86_64")
        self.assertEqual((layout.srcarch, layout.includes[:3]), ("x86", ["arch/x86/include/", "arch/x86/include/generated/", "include/"]))
        self.assertEqual(layout.arch_trees(), {"x86"})


if __name__ == "__main__":
    unittest.main()
