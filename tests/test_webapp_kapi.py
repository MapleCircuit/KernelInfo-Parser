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
