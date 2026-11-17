"""tests/test_webapp_hover.py - Hover card signature and kernel-doc extraction (G04, webapp/API.md section 4.3)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.backend.services.hover_service import extract_doc, extract_signature

SRC = """/**
 * list_add - add a new entry
 * @new: new entry to be added
 * @head: list head to add it after
 *
 * Insert a new entry after the specified head.
 * This is good for implementing stacks.
 *
 * More text.
 */
static inline void list_add(struct list_head *new,
			    struct list_head *head)
{
	__list_add(new, head, head->next);
}

/* plain comment */
#define container_of(ptr, type, member) ({			\\
	const typeof( ((type *)0)->member ) *__mptr = (ptr);	\\
	(type *)( (char *)__mptr - offsetof(type,member) );})

struct inode {
	umode_t i_mode;
};
extern void *kmalloc(size_t size, gfp_t flags);
""".split("\n")


class TestHover(unittest.TestCase):
    def test_signatures(self) -> None:
        self.assertEqual(extract_signature(SRC, 10), "static inline void list_add(struct list_head *new,\n\t\t\t    struct list_head *head)")
        self.assertEqual(extract_signature(SRC, 17).count("\n"), 2)
        self.assertTrue(extract_signature(SRC, 17).startswith("#define container_of(ptr, type, member)"))
        self.assertEqual(extract_signature(SRC, 21), "struct inode { … }")
        self.assertEqual(extract_signature(SRC, 24), "extern void *kmalloc(size_t size, gfp_t flags);")

    def test_kernel_doc(self) -> None:
        doc = extract_doc(SRC, 10)
        self.assertEqual(doc["summary"], "add a new entry")
        self.assertEqual([p["name"] for p in doc["params"]], ["new", "head"])
        self.assertEqual(doc["description"], "Insert a new entry after the specified head. This is good for implementing stacks.")
        self.assertTrue(doc["more"])
        self.assertEqual(doc["line"], 1)
        self.assertIsNone(extract_doc(SRC, 17), "a plain comment is not kernel-doc")
        self.assertIsNone(extract_doc(SRC, 21))

    def test_rust_signatures_and_doc(self) -> None:
        rust_src = [
            "/// Returns device status or error.",
            "///",
            "/// Extra description here.",
            "pub fn probe(dev: &mut PciDevice) -> Result<()> {",
            "    Ok(())",
            "}",
            "pub trait DeviceOperations {",
            "    fn init(&self) -> bool;",
            "}",
        ]
        doc = extract_doc(rust_src, 3)
        self.assertIsNotNone(doc)
        self.assertEqual(doc["summary"], "Returns device status or error.")
        self.assertEqual(doc["description"], "Extra description here.")
        self.assertEqual(doc["line"], 1)

        sig_fn = extract_signature(rust_src, 3)
        self.assertEqual(sig_fn, "pub fn probe(dev: &mut PciDevice) -> Result<()>")

        sig_trait = extract_signature(rust_src, 6)
        self.assertEqual(sig_trait, "pub trait DeviceOperations { … }")

        sig_method = extract_signature(rust_src, 7)
        self.assertEqual(sig_method, "    fn init(&self) -> bool;")


class TestPositionAwareHover(unittest.TestCase):
    """Router hover/detail/xref with `from_file`+`line`+`col` resolve the local scope; without `line` the global path runs."""

    def setUp(self) -> None:
        from unittest.mock import MagicMock, patch
        from tests import test_webapp_scope as ts
        from webapp.backend.routers import symbols as router
        from webapp.backend.services import hover_service as hs
        from webapp.backend.services import scope_service as sc
        self.ts, self.router, self.hs = ts, router, hs
        case = ts._ScopeCase("setUp")
        case.setUp()
        self.addCleanup(case.loaded.stop)
        self.case = case
        self.src = case.sources[ts.SYSCALL]
        self.global_hover = MagicMock(return_value={"global": True})
        for p in (patch.object(sc, "scope_service", case.svc),
                  patch.object(hs.hover_service, "hover", self.global_hover)):
            p.start()
            self.addCleanup(p.stop)

    def hover(self, name: str, line: int, nth: int = 0) -> dict:
        col = self.ts._col(self.src, line, name, nth)
        return self.router.get_symbol_hover("v3.0", name, "x86_64", self.ts.SYSCALL, line, col)

    def test_dev_local_card(self) -> None:
        card = self.hover("dev", 98)
        self.assertEqual((card["kind"], card["signature"], card["line"]), ("local", "struct pci_dev *dev", 89))
        self.assertEqual([u["line"] for u in card["scope"]["uses"]], [89, 98, 99, 107, 116, 125, 134])
        self.global_hover.assert_not_called()

    def test_byte_and_word(self) -> None:
        byte = self.hover("byte", 104)
        self.assertEqual(byte["signature"], "u8 byte")
        self.assertEqual(byte["scope"]["access"], "w")
        self.assertEqual(self.hover("word", 113)["signature"], "u16 word")

    def test_detail_and_xref_positioned(self) -> None:
        col = self.ts._col(self.src, 98, "dev")
        detail = self.router.get_symbol_detail("v3.0", "dev", "x86_64", self.ts.SYSCALL, 98, col)
        self.assertEqual(detail["line_s"], 89)
        xref = self.router.get_symbol_xref("v3.0", "dev", None, "x86_64", self.ts.SYSCALL, 98, col)
        self.assertEqual(xref["definition"]["line_s"], 89)

    def test_without_line_takes_global_path(self) -> None:
        out = self.router.get_symbol_hover("v3.0", "dev", "x86_64", self.ts.SYSCALL)
        self.assertEqual(out, {"global": True})
        self.global_hover.assert_called_once()


if __name__ == "__main__":
    unittest.main()
