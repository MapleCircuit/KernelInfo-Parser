"""tests/test_webapp_devices.py - Hardware -> driver device index (N4, webapp/API.md section 4.9)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.backend.services import device_index_service as d

SRC = """#define E1000_DEV_ID_82574L 0x10D3
static DEFINE_PCI_DEVICE_TABLE(e1000_pci_tbl) = {
	{ PCI_VDEVICE(INTEL, E1000_DEV_ID_82574L), board_82574 },
	{ PCI_DEVICE(0x10ec, 0x8139), .class = PCI_CLASS_NETWORK_ETHERNET << 8, .class_mask = 0xffff00 },
	{ PCI_DEVICE_CLASS(PCI_CLASS_STORAGE_SATA_AHCI, 0xffffff) },
	{ }	/* terminate list */
};
MODULE_DEVICE_TABLE(pci, e1000_pci_tbl);
static const struct of_device_id x_of_match[] = { { .compatible = "fsl," "imx6q-uart", }, {}, };
static const struct acpi_device_id x_acpi[] = { {"PNP0C0A", 0}, {"", 0} };
static struct usb_device_id usb_ids[] = { { USB_DEVICE(0x046d, 0xc52b) }, { } };
"""
DEFINES = {**d.parse_defines(SRC), "PCI_VENDOR_ID_INTEL": "0x8086", "PCI_CLASS_NETWORK_ETHERNET": "0x0200", "PCI_CLASS_STORAGE_SATA_AHCI": "0x010601"}


def entries(name: str, bus: str) -> list[dict]:
    init, _ = d.table_initializer(SRC.split("\n"), name)
    return [{**d.parse_entry(bus, e, DEFINES), "bus": bus, "table": name, "path": "x.c", "line": 1} for e in d.entries_of(init)]


class TestDeviceIndex(unittest.TestCase):
    def test_tables_and_entries(self) -> None:
        pci = entries("e1000_pci_tbl", "pci")
        self.assertEqual(len(pci), 3)
        self.assertEqual((pci[0]["vendor"], pci[0]["device"]), (0x8086, 0x10D3))
        self.assertEqual((pci[1]["cls"], pci[1]["mask"]), (0x020000, 0xFFFF00))
        self.assertEqual((pci[2]["cls"], pci[2]["mask"]), (0x010601, 0xFFFFFF))
        self.assertEqual(entries("x_of_match", "of")[0]["compatible"], "fsl,imx6q-uart")
        self.assertEqual(entries("x_acpi", "acpi")[0]["acpi_id"], "PNP0C0A")
        self.assertEqual(entries("usb_ids", "usb")[0]["device"], 0xC52B)

    def test_input_parsing(self) -> None:
        self.assertEqual(d.parse_input_line("00:19.0 Ethernet controller [0200]: Intel [8086:10d3] (rev 06)"),
                         [{"bus": "pci", "vendor": 0x8086, "device": 0x10D3, "cls": 0x020000}])
        self.assertEqual(d.parse_input_line("Bus 001 Device 002: ID 046d:c52b Logitech")[0]["device"], 0xC52B)
        self.assertEqual(d.parse_input_line("pci:v00008086d000010D3sv00001028sd000004AAbc02sc00i00")[0]["subdevice"], 0x04AA)
        self.assertEqual(d.parse_input_line("usb:v046DpC52Bd1201dc00"), [{"bus": "usb", "vendor": 0x046D, "device": 0xC52B}])
        self.assertEqual(d.parse_input_line("of:NserialT(null)Cfsl,imx6q-uart"), [{"bus": "of", "compatible": "fsl,imx6q-uart"}])
        self.assertEqual(d.parse_input_line("acpi:PNP0C0A:"), [{"bus": "acpi", "acpi_id": "PNP0C0A"}])
        self.assertEqual(len(d.parse_input_line("8086:10d3")), 2)
        self.assertEqual(d.parse_input_line("hello world"), [])

    def test_matching(self) -> None:
        pci = entries("e1000_pci_tbl", "pci")
        kinds = lambda ident: [k for k, _ in d.match(pci, ident)]  # noqa: E731
        self.assertEqual(kinds({"bus": "pci", "vendor": 0x8086, "device": 0x10D3}), ["exact"])
        self.assertEqual(kinds({"bus": "pci", "vendor": 0x10EC, "device": 0x8139, "cls": 0x020000}), ["exact"])
        self.assertEqual(kinds({"bus": "pci", "vendor": 0x10EC, "device": 0x8139, "cls": 0x030000}), [])  # class mask refuses
        self.assertEqual(kinds({"bus": "pci", "vendor": 0x1234, "device": 0x1, "cls": 0x010601}), ["class"])
        self.assertEqual(kinds({"bus": "pci", "vendor": 0x1234, "device": 0x1}), [])


if __name__ == "__main__":
    unittest.main()
