"""tests/test_webapp_kapi.py - Kernel API catalog parsers (A1-A5, webapp/API.md section 4.9)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.backend.services import kapi_service as k


class TestKapiParsers(unittest.TestCase):
    def test_source_reading(self) -> None:
        lines = ['SYSCALL_DEFINE3(read, unsigned int, fd,', '\t\tchar __user *, buf, /* x, y */ size_t, count)', "{"]
        body = k.invocation(lines, 1)
        self.assertEqual(k.split_args(body), ["read", "unsigned int", "fd", "char __user *", "buf", "size_t", "count"])
        self.assertEqual(k.split_args('a, f(b, c), "x,y", {1, 2}'), ["a", "f(b, c)", '"x,y"', "{1, 2}"])
        self.assertEqual(k.string_value('"noapic" "="'), "noapic=")
        self.assertIsNone(k.string_value("NAME"))
        self.assertEqual(k.invocation(["f(a, ')' , b)"], 1), "a, ')' , b")
        self.assertEqual(k.invocation(["f(a"], 1), "")

    def test_syscall_sources(self) -> None:
        tbl = "# comment\n0\tcommon\tread\t\t\tsys_read\n512\tx32\trt_sigaction\tcompat_sys_rt_sigaction\n13\t64\trt_sigaction\tsys_rt_sigaction\n"
        rows = k.parse_syscall_tbl(tbl)
        self.assertEqual([(r["nr"], r["abi"], r["name"], r["entry"]) for r in rows][:1], [(0, "common", "read", "sys_read")])
        self.assertEqual(len(rows), 3)
        hdr = "#define __NR_read 0\n__SYSCALL(__NR_read, sys_read)\n#define __NR_write 1\n__SYSCALL(__NR_write, sys_write)\n#define __NR_syscalls 2\n"
        self.assertEqual([(r["nr"], r["name"], r["entry"]) for r in k.parse_unistd_header(hdr)], [(0, "read", "sys_read"), (1, "write", "sys_write")])
        self.assertEqual(k.parse_syscall_table_s("ENTRY(sys_call_table)\n\t.long sys_restart_syscall\n\t.long sys_exit\n"), ["sys_restart_syscall", "sys_exit"])

    def test_unistd_header_word_size(self) -> None:
        hdr = ("#define __NR3264_mmap 222\n__SC_3264(__NR3264_mmap, sys_mmap2, sys_mmap)\n"
               "#define __NR_read 63\n__SYSCALL(__NR_read, sys_read)\n"
               "#if __BITS_PER_LONG == 64 && !defined(__SYSCALL_COMPAT)\n#define __NR_mmap __NR3264_mmap\n"
               "#else\n#define __NR_mmap2 __NR3264_mmap\n#endif\n"
               "#if defined(__ARCH_WANT_X) || \\\n __BITS_PER_LONG != 32\n#define __NR_time64only 7\n#endif\n"
               "#ifdef __ARCH_WANT_Y\n#define __NR_maybe 8\n#else\n#define __NR_other 9\n#endif\n#define __NR_syscalls 300\n")
        rows64 = k.parse_unistd_header(k.bits_conditional(hdr, False), False)
        rows32 = k.parse_unistd_header(k.bits_conditional(hdr, True), True)
        self.assertEqual({(r["nr"], r["name"], r["entry"]) for r in rows64},
                         {(63, "read", "sys_read"), (222, "mmap", "sys_mmap"), (7, "time64only", "sys_time64only"),
                          (8, "maybe", "sys_maybe"), (9, "other", "sys_other")})
        self.assertEqual({(r["nr"], r["name"], r["entry"]) for r in rows32},
                         {(63, "read", "sys_read"), (222, "mmap2", "sys_mmap2"), (7, "time64only", "sys_time64only"),
                          (8, "maybe", "sys_maybe"), (9, "other", "sys_other")})
        mips = "#define __NR_Linux 4000\n#if _MIPS_SIM == _MIPS_SIM_ABI32\n#define __NR_exit (__NR_Linux + 1)\n#endif\n#define __NR_Linux_syscalls 343\n"
        self.assertEqual([(r["nr"], r["name"]) for r in k.parse_unistd_header(
            k.bits_conditional(mips, True, {"_MIPS_SIM": 1, "_MIPS_SIM_ABI32": 1}))], [(4001, "exit")])
        self.assertEqual(k.parse_unistd_header(k.bits_conditional(mips, False, {"_MIPS_SIM": 3, "_MIPS_SIM_ABI32": 1})), [])
        self.assertIsNone(k._pp_eval("defined(CONFIG_FOO)", True))
        self.assertTrue(k._pp_eval("!(__BITS_PER_LONG == 32)", False))

    def test_numbered_elsewhere(self) -> None:
        files_text = {
            "arch/x86/syscalls/syscall_64.tbl": "0 common read sys_read\n1 64 arch_prctl sys_arch_prctl\n",
            "arch/x86/syscalls/syscall_32.tbl": "0 i386 read sys_read\n1 i386 ipc sys_ipc\n2 i386 old sys_ni_syscall\n",
            "arch/arm/tools/syscall.tbl": "0 common read sys_read\n1 oabi ipc sys_ipc\n2 common fork sys_fork\n",
            "arch/riscv/kernel/Makefile.syscalls": "syscall_abis_64 += riscv\n",
            "scripts/syscall.tbl": "0 common read sys_read\n1 riscv flush sys_riscv_flush_icache\n2 32 mmap2 sys_mmap2\n3 rlimit setrlimit sys_setrlimit\n",
            "arch/um/Makefile": "",
        }

        class Files:
            sha = "x"

            def lines(self, path: str) -> list[str]:
                return files_text.get(path, "").split("\n")

        paths = set(files_text)
        el = k._numbered_elsewhere(Files(), paths, "x86_64", "x86")
        self.assertEqual(sorted(el["sys_ipc"]), ["i386"])
        self.assertEqual(sorted(el["sys_read"]), ["arm", "i386", "riscv"])
        self.assertEqual(sorted(el["sys_riscv_flush_icache"]), ["riscv"])
        self.assertEqual(sorted(el["sys_mmap2"]), ["riscv"])  # rv32: common + 32
        self.assertNotIn("sys_setrlimit", el)  # an ABI riscv does not list
        self.assertNotIn("sys_old", el)  # sys_ni_syscall
        self.assertNotIn("um", {a for v in el.values() for a in v})
        self.assertEqual(sorted(k._numbered_elsewhere(Files(), paths, "i386", "x86")["sys_arch_prctl"]), ["x86_64"])

    def test_exports(self) -> None:
        self.assertEqual(k.parse_export("EXPORT_SYMBOL(kmalloc);"), {"symbol": "kmalloc", "variant": "plain", "namespace": ""})
        self.assertEqual(k.parse_export("EXPORT_SYMBOL_GPL(kfree_rcu);")["variant"], "gpl")
        self.assertEqual(k.parse_export('EXPORT_SYMBOL_NS_GPL(usb_x, "USB_STORAGE");'), {"symbol": "usb_x", "variant": "ns_gpl", "namespace": "USB_STORAGE"})
        self.assertEqual(k.parse_export("EXPORT_UNUSED_SYMBOL_GPL(old);")["variant"], "unused_gpl")
        self.assertIsNone(k.parse_export("/* EXPORT_SYMBOL is a macro */"))

    def test_kernel_parameters_doc(self) -> None:
        doc = "Intro\n\n\tacpi=\t\t[HW,ACPI,X86]\n\t\t\tAdvanced Configuration\n\t\t\tFormat: { force | off }\n\n\tnoapic\t\t[SMP,APIC] Tells the kernel\n"
        d = k.parse_kernel_parameters_doc(doc)
        self.assertEqual(d["acpi"]["tags"], ["HW", "ACPI", "X86"])
        self.assertTrue(d["acpi"]["takes_value"])
        self.assertIn("Format", d["acpi"]["text"])
        self.assertEqual(d["noapic"]["line"], 7)

    def test_tracepoints(self) -> None:
        body = ("sched_switch,\n TP_PROTO(struct task_struct *prev, struct task_struct *next),\n TP_ARGS(prev, next),\n"
                " TP_STRUCT__entry(\n __array( char, prev_comm, TASK_COMM_LEN )\n __field( pid_t, prev_pid )\n __string( name, dev )\n ),\n"
                " TP_fast_assign( x ),\n TP_printk(\"prev_pid=%d\", __entry->prev_pid)")
        tp = k.parse_tracepoint("TRACE_EVENT", body)
        self.assertEqual(tp["name"], "sched_switch")
        self.assertEqual(tp["args"], "prev, next")
        self.assertEqual([(f["kind"], f["name"]) for f in tp["fields"]], [("array", "prev_comm"), ("field", "prev_pid"), ("string", "name")])
        self.assertTrue(tp["printk"].startswith('"prev_pid=%d"'))
        ev = k.parse_tracepoint("DEFINE_EVENT", "sched_class, sched_wakeup, TP_PROTO(int a), TP_ARGS(a)")
        self.assertEqual((ev["class"], ev["name"]), ("sched_class", "sched_wakeup"))


if __name__ == "__main__":
    unittest.main()


class TestApiDiff(unittest.TestCase):
    def test_compare(self) -> None:
        from webapp.backend.services.kapi_diff_service import compare
        a = {"x": {"variant": "plain", "namespace": "", "path": "a.c"}, "gone": {"variant": "gpl", "namespace": "", "path": "b.c"}}
        b = {"x": {"variant": "gpl", "namespace": "", "path": "a.c"}, "new": {"variant": "plain", "namespace": "", "path": "c.c"}}
        r = compare("exports", a, b)
        self.assertEqual([x["key"] for x in r["added"]], ["new"])
        self.assertEqual([x["key"] for x in r["removed"]], ["gone"])
        self.assertEqual(r["changed"], [{"key": "x", "field": "variant", "from": "plain", "to": "gpl", "path": "a.c"}])
        t = compare("tracepoints", {"e": {"fields": ["a", "b"], "printk": "x", "path": "t.h"}}, {"e": {"fields": ["b", "c"], "printk": "x", "path": "t.h"}})
        self.assertEqual(t["changed"], [{"key": "e", "fields_added": ["c"], "fields_removed": ["a"], "printk_changed": False, "path": "t.h"}])
