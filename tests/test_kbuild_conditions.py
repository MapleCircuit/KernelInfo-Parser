"""tests/test_kbuild_conditions.py - Kbuild build conditions (parser/kbuild_conditions.py)."""
from __future__ import annotations

import unittest

from parser.kbuild_conditions import KbuildConditionResolver, and_, evaluate, not_, or_

# A miniature v3.0-style tree: goal lists in the top/arch Makefiles, BITS exported by arch/x86/Makefile,
# Kbuild taking precedence over Makefile, composites, $(proc-y) list references, built-in.o links,
# #included sources and host programs.
TREE = {
    "Makefile": """
ifeq ($(mixed-targets),1)
all:
else
init-y := init/
drivers-y := drivers/
core-y := usr/
core-y += kernel/ security/
endif
""",
    "arch/x86/Makefile": """
export BITS
ifeq ($(CONFIG_X86_32),y)
        BITS := 32
else
        BITS := 64
endif
head-y := arch/x86/kernel/head_$(BITS).o
core-y += arch/x86/
drivers-$(CONFIG_PCI) += arch/x86/pci/
boot := arch/x86/boot
""",
    "arch/x86/Kbuild": "obj-y += kernel/\nobj-$(CONFIG_KVM) += kvm/\n",
    "arch/x86/kernel/Makefile": """
extra-y := head_$(BITS).o
obj-y := process_$(BITS).o setup.o
obj-$(CONFIG_X86_32) += tls.o
obj-$(CONFIG_IA32_EMULATION) += tls.o
ifdef CONFIG_SMP
obj-y += smp.o
else
obj-y += up.o
endif
""",
    "arch/x86/kernel/process_32.c": "", "arch/x86/kernel/process_64.c": "", "arch/x86/kernel/setup.c": "",
    "arch/x86/kernel/tls.c": "", "arch/x86/kernel/smp.c": "", "arch/x86/kernel/up.c": "",
    "arch/x86/kernel/head_64.S": "", "arch/x86/kernel/head_32.S": "", "arch/x86/kernel/orphan.c": "",
    "arch/x86/kvm/Makefile": "obj-$(CONFIG_KVM) += kvm.o\nkvm-y += x86.o\nkvm-$(CONFIG_KVM_MMU_AUDIT) += audit.o\n",
    "arch/x86/kvm/x86.c": "", "arch/x86/kvm/audit.c": "",
    "arch/x86/boot/Makefile": "targets := main.o\n", "arch/x86/boot/main.c": "",
    "arch/arm/kernel/Makefile": "obj-y += setup.o\n", "arch/arm/kernel/setup.c": "",
    "kernel/Makefile": "obj-y = sched.o\nobj-$(CONFIG_MODULES) += module.o\n",
    "kernel/sched.c": "", "kernel/module.c": "", "kernel/sched_fair.c": "",
    "drivers/Makefile": "obj-$(CONFIG_NET) += net/\n",
    "drivers/net/Makefile": """
obj-$(CONFIG_BONDING) += bonding/
obj-$(CONFIG_E1000) += e1000.o
e1000-objs := e1000_main.o $(addprefix hw/, e1000_hw.o)
obj-$(CONFIG_GEN) += $(shell ls *.o)
""",
    "drivers/net/e1000_main.c": "", "drivers/net/hw/e1000_hw.c": "", "drivers/net/skeleton.c": "",
    "drivers/net/bonding/Makefile": """
obj-$(CONFIG_BONDING) += bonding.o
bonding-objs := bond_main.o
proc-$(CONFIG_PROC_FS) += bond_procfs.o
bonding-objs += $(proc-y)
""",
    "drivers/net/bonding/bond_main.c": "", "drivers/net/bonding/bond_procfs.c": "", "drivers/net/bonding/bond_ipv6.c": "",
    "security/Makefile": "subdir-$(CONFIG_SECURITY_SELINUX) += selinux\nobj-$(CONFIG_SECURITY_SELINUX) += selinux/built-in.o\n",
    "security/selinux/Makefile": "obj-$(CONFIG_SECURITY_SELINUX) := selinux.o\nselinux-y := hooks.o\n",
    "security/selinux/hooks.c": "",
    "usr/Makefile": "hostprogs-y := gen_init_cpio\n", "usr/gen_init_cpio.c": "",
    "kernel/Kconfig.preempt": "", "include/linux/sched.h": "",
}

X86_64 = {"X86_64": "y", "X86_32": "n", "SMP": "y", "MODULES": "y", "NET": "y", "E1000": "m", "BONDING": "y",
          "PROC_FS": "y", "KVM": "n", "IA32_EMULATION": "y", "SECURITY_SELINUX": "y", "PCI": "y"}


def resolver(arch: str = "x86_64", srcarch: str = "x86", included: set[str] | None = None) -> KbuildConditionResolver:
    return KbuildConditionResolver(TREE.get, TREE.__contains__, arch, srcarch, included)


class TestKbuildConditions(unittest.TestCase):
    def built(self, path: str, values: dict[str, str] = X86_64, **kw) -> str:
        cond = resolver(**kw).file_condition(path)
        return evaluate(cond["expr"], lambda s: values.get(s, "n"))

    def test_bits_alternatives_from_the_arch_makefile(self) -> None:
        self.assertEqual(self.built("arch/x86/kernel/process_64.c"), "y")
        self.assertEqual(self.built("arch/x86/kernel/process_32.c"), "n")
        self.assertEqual(self.built("arch/x86/kernel/head_64.S"), "y")
        i386 = {**X86_64, "X86_64": "n", "X86_32": "y"}
        self.assertEqual(self.built("arch/x86/kernel/process_32.c", i386), "y")
        self.assertEqual(self.built("arch/x86/kernel/process_64.c", i386), "n")

    def test_conditionals_keep_their_polarity(self) -> None:
        self.assertEqual(self.built("arch/x86/kernel/smp.c"), "y")
        self.assertEqual(self.built("arch/x86/kernel/up.c"), "n")
        self.assertEqual(self.built("arch/x86/kernel/up.c", {**X86_64, "SMP": "n"}), "y")
        self.assertEqual(self.built("arch/x86/kernel/tls.c"), "y")  # IA32_EMULATION
        self.assertEqual(self.built("arch/x86/kernel/tls.c", {**X86_64, "IA32_EMULATION": "n"}), "n")

    def test_directories_composites_and_modules(self) -> None:
        self.assertEqual(self.built("kernel/sched.c"), "y")
        self.assertEqual(self.built("drivers/net/e1000_main.c"), "m")
        self.assertEqual(self.built("drivers/net/hw/e1000_hw.c"), "m")  # $(addprefix hw/, ...)
        self.assertEqual(self.built("drivers/net/e1000_main.c", {**X86_64, "NET": "n"}), "n")
        self.assertEqual(self.built("arch/x86/kvm/x86.c"), "n")
        self.assertEqual(self.built("arch/x86/kvm/x86.c", {**X86_64, "KVM": "y"}), "y")
        self.assertEqual(self.built("arch/x86/kvm/audit.c", {**X86_64, "KVM": "y"}), "n")
        self.assertEqual(self.built("security/selinux/hooks.c"), "y")  # selinux/built-in.o
        self.assertEqual(self.built("drivers/net/bonding/bond_procfs.c"), "y")  # $(proc-y)
        self.assertEqual(self.built("drivers/net/bonding/bond_procfs.c", {**X86_64, "PROC_FS": "n"}), "n")

    def test_other_arch_and_unbuilt_sources(self) -> None:
        r = resolver()
        self.assertEqual(r.file_condition("arch/arm/kernel/setup.c")["status"], "other_arch")
        self.assertEqual(r.file_condition("kernel/sched_fair.c")["status"], "no_rule")
        self.assertEqual(r.file_condition("drivers/net/bonding/bond_ipv6.c")["status"], "no_rule")
        self.assertEqual(resolver(arch="arm", srcarch="arm").file_condition("arch/arm/kernel/setup.c")["status"], "rule")

    def test_never_reported_unbuilt_when_unsure(self) -> None:
        r = resolver(included={"sched_fair.c"})
        self.assertEqual(r.file_condition("kernel/sched_fair.c")["status"], "unknown")  # #include "sched_fair.c"
        self.assertEqual(r.file_condition("arch/x86/boot/main.c")["status"], "unknown")  # own make target
        self.assertEqual(r.file_condition("usr/gen_init_cpio.c")["status"], "unknown")  # host program
        self.assertEqual(r.file_condition("drivers/net/skeleton.c")["status"], "unknown")  # $(shell ...) list
        self.assertEqual(r.file_condition("include/linux/sched.h")["status"], "unknown")
        self.assertEqual(r.file_condition("arch/x86/kernel/orphan.c")["status"], "no_rule")

    def test_expression_folding_and_unknowns(self) -> None:
        self.assertEqual(and_("y", {"sym": "A"}, "y"), {"sym": "A"})
        self.assertEqual(and_({"sym": "A"}, "n"), "n")
        self.assertEqual(or_({"sym": "A"}, "y"), "y")
        self.assertEqual(not_("m"), "n")
        self.assertEqual(evaluate(and_({"sym": "A"}, "?"), lambda s: "n"), "n")
        self.assertEqual(evaluate(and_({"sym": "A"}, "?"), lambda s: "y"), "?")
        self.assertEqual(evaluate(or_({"sym": "A"}, "?"), lambda s: "m"), "m")
        self.assertEqual(evaluate({"sym": "A"}, lambda s: None), "?")


if __name__ == "__main__":
    unittest.main()
