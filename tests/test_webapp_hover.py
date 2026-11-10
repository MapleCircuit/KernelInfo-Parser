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


if __name__ == "__main__":
    unittest.main()
