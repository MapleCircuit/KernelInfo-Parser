"""webapp/backend/services/device_index_service.py - Hardware -> driver (N4, webapp/API.md section 4.9).

Every `MODULE_DEVICE_TABLE(<bus>, <table>)` of a revision, from the Kernel API records of its blobs
(`parser/kapi_facts.py`: the table's initializer entries, the file's quoted includes and #defines; see
`kapi_service.revision_records`), parsed into device ids (PCI, USB, OF, ACPI; other buses keep the entry text),
symbolic constants resolved from the driver's own #defines, the headers of its directory it includes (read here, each
once), and include/linux/pci_ids.h. Built per commit by a background job (section 2.15), cached on disk (section 2.14). Pasted `lspci -nn`, `lsusb`,
modalias, compatible or ACPI ids are matched against it; each match names the module and its options
(Kbuild object index, section 4.4).
"""
from __future__ import annotations

import logging
import os
import re
from collections import ChainMap
from typing import Any, Callable

from fastapi import HTTPException

# entries_of, parse_defines, table_initializer are re-exported: the device index readers (tests)
from parser.kapi_facts import entries_of, parse_defines, quoted_includes, split_args, string_value, table_initializer  # noqa: F401
from webapp.backend.config import get_active_repo_name
from webapp.backend.services.disk_cache import DiskCache
from webapp.backend.services.jobs import jobs
from webapp.backend.services.kapi_service import FileCache, resolve, revision_records

logger = logging.getLogger(__name__)

INDEX_FORMAT = 3
_DISK = DiskCache("device-index", max_bytes=256 << 20)
PCI_ANY = 0xFFFFFFFF
MAX_INPUT = 64 * 1024

# ------------------------------------------------------------------------------- constants


def to_int(token: str, defines: Any, depth: int = 0) -> int | None:
    t = (token or "").strip().strip("()").strip()
    if not t:
        return None
    if re.fullmatch(r"0[xX][0-9a-fA-F]+[uUlL]*", t):
        return int(t.rstrip("uUlL"), 16)
    if re.fullmatch(r"\d+[uUlL]*", t):
        return int(t.rstrip("uUlL"))
    if t == "PCI_ANY_ID":
        return PCI_ANY
    if depth < 4 and t in defines:
        return to_int(defines[t], defines, depth + 1)
    m = re.fullmatch(r"(\w+)\s*<<\s*(\d+)", t)
    if m:
        base = to_int(m.group(1), defines, depth + 1)
        return None if base is None else base << int(m.group(2))
    return None


# ------------------------------------------------------------------------------- tables

def parse_entry(bus: str, entry: str, defines: Any) -> dict[str, Any]:
    body = entry[1:-1].strip() if entry.startswith("{") and entry.endswith("}") else entry
    rec: dict[str, Any] = {"text": re.sub(r"\s+", " ", entry)[:200]}
    if bus == "pci":
        m = re.search(r"\bPCI_(V?)DEVICE(?:_SUB)?\s*\(([^)]*)\)", body)
        if m:
            args = [a.strip() for a in m.group(2).split(",")]
            vendor = f"PCI_VENDOR_ID_{args[0]}" if m.group(1) == "V" else args[0]
            rec.update(vendor=to_int(vendor, defines), device=to_int(args[1], defines) if len(args) > 1 else None)
            if len(args) >= 4:
                rec.update(subvendor=to_int(args[2], defines), subdevice=to_int(args[3], defines))
            named = dict(re.findall(r"\.(class|class_mask)\s*=\s*([^,}]+)", body))
            if named:
                rec.update(cls=to_int(named.get("class", ""), defines), mask=to_int(named.get("class_mask", ""), defines))
            return rec
        m = re.search(r"\bPCI_DEVICE_CLASS\s*\(([^,]+),([^)]+)\)", body)
        if m:
            rec.update(cls=to_int(m.group(1), defines), mask=to_int(m.group(2), defines))
            return rec
        named = dict(re.findall(r"\.(vendor|device|subvendor|subdevice|class|class_mask)\s*=\s*([^,}]+)", body))
        if named:
            rec.update(vendor=to_int(named.get("vendor", ""), defines), device=to_int(named.get("device", ""), defines),
                       cls=to_int(named.get("class", ""), defines), mask=to_int(named.get("class_mask", ""), defines))
            return rec
        args = split_args(body)
        if len(args) >= 2:
            rec.update(vendor=to_int(args[0], defines), device=to_int(args[1], defines))
            if len(args) >= 4:
                rec.update(subvendor=to_int(args[2], defines), subdevice=to_int(args[3], defines))
            if len(args) >= 6:
                rec.update(cls=to_int(args[4], defines), mask=to_int(args[5], defines))
        return rec
    if bus == "usb":
        m = re.search(r"\bUSB_DEVICE\w*\s*\(([^,]+),([^,)]+)", body)
        if m:
            rec.update(vendor=to_int(m.group(1), defines), device=to_int(m.group(2), defines))
            return rec
        m = re.search(r"\bUSB_INTERFACE_INFO\s*\(([^,]+),([^,]+),([^)]+)\)", body)
        if m:
            rec.update(cls=to_int(m.group(1), defines))
            return rec
        named = dict(re.findall(r"\.(idVendor|idProduct|bInterfaceClass)\s*=\s*([^,}]+)", body))
        if named:
            rec.update(vendor=to_int(named.get("idVendor", ""), defines), device=to_int(named.get("idProduct", ""), defines),
                       cls=to_int(named.get("bInterfaceClass", ""), defines))
        return rec
    if bus == "of":
        m = re.search(r"\.compatible\s*=\s*(\"(?:[^\"\\]|\\.)*\"(?:\s*\"(?:[^\"\\]|\\.)*\")*)", body)
        if m:
            rec["compatible"] = string_value(m.group(1))
        return rec
    if bus == "acpi":
        args = split_args(body)
        ident = string_value(args[0]) if args else None
        m = re.search(r"\.id\s*=\s*(\"[^\"]*\")", body)
        rec["acpi_id"] = string_value(m.group(1)) if m else ident
        return rec
    return rec


def build_index(sha: str, progress: Callable | None = None) -> dict[str, Any]:
    files = FileCache(sha)
    headers: dict[str, tuple[str, list[str], dict[str, str]]] = {}  # path -> (text, quoted includes, defines)

    def header(path: str) -> tuple[str, list[str], dict[str, str]]:
        if path not in headers:
            text = "\n".join(files.lines(path))
            headers[path] = (text, quoted_includes(text), parse_defines(text)) if text else ("", [], {})
        return headers[path]

    pci_ids = header("include/linux/pci_ids.h")[2]
    records = [(path, rec) for path, rec in revision_records(sha, progress) if rec.get("v") and path.endswith(".c")]
    tables = 0
    entries = []
    for n, (path, rec) in enumerate(records):
        if progress and n % 200 == 0:
            progress(n, len(records), "device tables")
        # The driver's own headers (#include "…", followed 3 levels: netdev.c -> e1000.h -> hw.h); the nearer
        # include wins, the driver's own #defines over all of them, include/linux/pci_ids.h last
        seen = {path}
        frontier = [(path, rec.get("vi") or [])]
        local: list[dict[str, str]] = []
        for _ in range(3):
            nxt = []
            for src, includes in frontier:
                for inc in includes:
                    hdr = os.path.normpath(os.path.join(os.path.dirname(src), inc))
                    if hdr in seen:
                        continue
                    seen.add(hdr)
                    text, hdr_includes, hdr_defines = header(hdr)
                    if text:
                        local.append(hdr_defines)
                        nxt.append((hdr, hdr_includes))
            frontier = nxt
        defines = ChainMap(rec.get("vd") or {}, *local, pci_ids)
        for bus, name, line, table_entries in rec["v"]:
            tables += 1
            if table_entries is None:
                continue
            for e in table_entries:
                entries.append({**parse_entry(bus, e, defines), "bus": bus, "table": name, "path": path, "line": line})
    return {"entries": entries, "tables": tables}


def index_state(version: str, wait: float = 0.0) -> dict[str, Any]:
    sha = resolve(version)
    ck = f"{get_active_repo_name()}|devices|{INDEX_FORMAT}|{sha}"
    state = jobs.submit(f"devices:{ck}", lambda p: build_index(sha, p), cache=_DISK, cache_key=ck)
    if state["state"] == "building" and wait > 0:
        state = jobs.wait(f"devices:{ck}", wait) or state
    return state


# ------------------------------------------------------------------------------- input parsing and matching

def parse_input_line(line: str) -> list[dict[str, Any]]:
    """Device ids named by one pasted line (lspci -nn, lsusb, modalias, vendor:device, compatible, ACPI)."""
    s = line.strip()
    if not s:
        return []
    m = re.match(r"^pci:v([0-9A-Fa-f]{8})d([0-9A-Fa-f]{8})(?:sv([0-9A-Fa-f]{8})sd([0-9A-Fa-f]{8}))?(?:bc([0-9A-Fa-f]{2})sc([0-9A-Fa-f]{2})i([0-9A-Fa-f]{2}))?", s)
    if m:
        ident = {"bus": "pci", "vendor": int(m.group(1), 16), "device": int(m.group(2), 16)}
        if m.group(3):
            ident.update(subvendor=int(m.group(3), 16), subdevice=int(m.group(4), 16))
        if m.group(5):
            ident["cls"] = int(m.group(5) + m.group(6) + m.group(7), 16)
        return [ident]
    m = re.match(r"^usb:v([0-9A-Fa-f]{4})p([0-9A-Fa-f]{4})", s)
    if m:
        return [{"bus": "usb", "vendor": int(m.group(1), 16), "device": int(m.group(2), 16)}]
    m = re.match(r"^of:.*?C([^C\s]+)", s)
    if m:
        return [{"bus": "of", "compatible": m.group(1)}]
    m = re.match(r"^acpi:([A-Z0-9]{7,8}):", s)
    if m:
        return [{"bus": "acpi", "acpi_id": m.group(1)}]
    m = re.search(r"\bID ([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\b", s)  # lsusb
    if m:
        return [{"bus": "usb", "vendor": int(m.group(1), 16), "device": int(m.group(2), 16)}]
    ids = re.findall(r"\[([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\]", s)  # lspci -nn (class [0200] has no colon)
    if ids:
        out = [{"bus": "pci", "vendor": int(ids[0][0], 16), "device": int(ids[0][1], 16)}]
        cm = re.search(r"\[([0-9a-fA-F]{4})\]", s)
        if cm:
            out[0]["cls"] = int(cm.group(1), 16) << 8
        return out
    m = re.fullmatch(r"(?:0x)?([0-9a-fA-F]{4}):(?:0x)?([0-9a-fA-F]{4})", s)
    if m:
        v, d = int(m.group(1), 16), int(m.group(2), 16)
        return [{"bus": "pci", "vendor": v, "device": d}, {"bus": "usb", "vendor": v, "device": d}]
    m = re.fullmatch(r"[\"']?([\w.+-]+,[\w.+-]+)[\"']?", s)
    if m:
        return [{"bus": "of", "compatible": m.group(1)}]
    m = re.fullmatch(r"[A-Z][A-Z0-9]{2,3}[0-9A-F]{4}", s)
    if m:
        return [{"bus": "acpi", "acpi_id": s}]
    return []


def _field_ok(entry_val: int | None, dev_val: int | None) -> bool | None:
    """Kernel semantics of one id field: ANY matches; an unresolved entry field matches nothing."""
    if entry_val == PCI_ANY:
        return True
    if entry_val is None or dev_val is None:
        return None
    return entry_val == dev_val


def _pci_match(e: dict[str, Any], ident: dict[str, Any]) -> str | None:
    """pci_match_one_device(): vendor, device, subvendor, subdevice (ANY wildcards) and class under its
    mask; the kind says how specific the entry is (exact > subsystem > vendor > class)."""
    v, d = e.get("vendor"), e.get("device")
    if "vendor" in e and v is None:
        return None  # unresolved constant: never guess
    if v is not None and _field_ok(v, ident.get("vendor")) is not True:
        return None
    if d is not None and _field_ok(d, ident.get("device")) is not True:
        return None
    if "device" in e and d is None:
        return None
    for key in ("subvendor", "subdevice"):
        if e.get(key) not in (None, PCI_ANY) and _field_ok(e[key], ident.get(key)) is not True:
            return None
    mask = e.get("mask") or 0
    if mask:
        if ident.get("cls") is None or ((e.get("cls") or 0) ^ ident["cls"]) & mask:
            return None
    if v in (None, PCI_ANY) and d in (None, PCI_ANY):
        return "class" if mask else None
    if d in (None, PCI_ANY):
        return "vendor"
    if e.get("subvendor") not in (None, PCI_ANY) or e.get("subdevice") not in (None, PCI_ANY):
        return "subsystem"
    return "exact"


def match(entries: list[dict[str, Any]], ident: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Entries matching an id, best first: (match kind, entry)."""
    out = []
    bus = ident["bus"]
    for e in entries:
        if e["bus"] != bus:
            continue
        kind = None
        if bus in ("pci", "usb"):
            kind = _pci_match(e, ident)
        elif bus == "of":
            if e.get("compatible") and e["compatible"] == ident.get("compatible"):
                kind = "exact"
        elif bus == "acpi":
            if e.get("acpi_id") and e["acpi_id"].upper() == str(ident.get("acpi_id", "")).upper():
                kind = "exact"
        if kind:
            out.append((kind, e))
    rank = {"exact": 0, "subsystem": 1, "vendor": 2, "class": 3}
    out.sort(key=lambda x: rank[x[0]])
    return out


def _module_of(kidx: dict[str, Any] | None, path: str) -> dict[str, Any]:
    from webapp.backend.services.kbuild_index_service import objects_for_file, required_symbols
    if not kidx:
        return {"module": os.path.splitext(os.path.basename(path))[0], "options": [], "expr": None}
    objs = objects_for_file(kidx, path)
    if not objs:
        return {"module": "", "options": [], "expr": None}
    o = objs[0]
    return {"module": o["modname"], "target": o["target"], "options": required_symbols(o["expr"]), "expr": o["expr"]}


def fmt_id(e: dict[str, Any]) -> str:
    if e["bus"] in ("pci", "usb") and e.get("vendor") is not None and e.get("device") is not None:
        dev = "*" if e["device"] == PCI_ANY else f"{e['device']:04x}"
        return f"{e['vendor']:04x}:{dev}"
    if e.get("cls") is not None:
        return f"class {e['cls']:06x}"
    return e.get("compatible") or e.get("acpi_id") or e.get("text", "")


def match_text(version: str, text: str, arch: str) -> dict[str, Any]:
    if len(text.encode()) > MAX_INPUT:
        raise HTTPException(status_code=400, detail="At most 64 KB of input")
    st = index_state(version, wait=60.0)
    if st.get("state") != "done":
        raise HTTPException(status_code=503, detail="The device index is still being built, retry")
    from webapp.backend.services.kapi_service import _kbuild_files
    kidx = _kbuild_files(version, arch)
    entries = st["result"]["entries"]
    lines_out, unmatched = [], []
    for raw in text.splitlines()[:2000]:
        ids = parse_input_line(raw)
        if not ids:
            continue
        matches = []
        seen = set()
        for ident in ids:
            for kind, e in match(entries, ident)[:20]:
                key = (e["path"], e["table"])
                if key in seen:
                    continue
                seen.add(key)
                matches.append({"match_kind": kind, "bus": e["bus"], "id": fmt_id(e), "table": e["table"], "path": e["path"],
                                "line": e["line"], **_module_of(kidx, e["path"])})
        lines_out.append({"input": raw.strip()[:300], "ids": [fmt_id(i) for i in ids], "matches": matches})
        if not matches:
            unmatched.append(raw.strip()[:300])
    return {"version": version, "arch": arch, "lines": lines_out, "unmatched": unmatched}
