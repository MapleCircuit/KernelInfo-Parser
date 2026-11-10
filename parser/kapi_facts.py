"""parser/kapi_facts.py - The Kernel API macros of one source file, read from its text.

Pure functions, no I/O, run once per Git blob (content-addressed, like `parser/source_facts.py` and in the same
worker pass): by the parser for every `.c` / `.h` / `.S` blob of an ingested version (Step 6.9, stored in
`m_blob_facts` as family `FAMILY` (2), `core/API.md` section 6), and by the webapp for blobs of revisions the parser
never ingested (`webapp/backend/services/kapi_service.py`, `webapp/API.md` section 4.9). The webapp assembles the
Kernel API catalogs of a revision (A1-A5, N4) from these records instead of searching its whole tree.

`extract(text)` -> a dict holding only the keys that have entries (`{}` for most files):
    "x": [[symbol, variant, namespace, line], ...]          EXPORT_SYMBOL* at a line start
    "s": [[macro, line, [args...]], ...]                     SYSCALL_DEFINEn / COMPAT_SYSCALL_DEFINEn at a line start
    "p": [[macro, line, body, [args...]], ...]               __setup, early_param, core_param, module_param*,
                                                             MODULE_PARM_DESC (`#define` lines left out, except
                                                             MODULE_PARM_DESC); `body` is cut at 400 characters
    "m": [[macro, line, [args...]], ...]                     MODULE_LICENSE/AUTHOR/DESCRIPTION/ALIAS/FIRMWARE/
                                                             DEVICE_TABLE/VERSION/SOFTDEP..., module_init, module_exit,
                                                             module_<bus>_driver (first 200 lines per file)
    "e": [[line, tracepoint], ...]                           TRACE_EVENT*, DEFINE_EVENT*, DECLARE_EVENT_CLASS,
                                                             DECLARE_TRACE at a line start (`parse_tracepoint()`)
    "ts": the file's `#define TRACE_SYSTEM` ("" when none)   (with "e")
    "v": [[bus, table, line, [entry, ...] | None], ...]      MODULE_DEVICE_TABLE(bus, table) (first 50 per file) with
                                                             its initializer's entries (None: initializer not found)
    "vi": the file's first 20 `#include "..."` names         (with "v")
    "vd": the file's simple `#define NAME value` constants   (with "v", `parse_defines()`)

Lines are chosen with the line patterns the catalogs used with `git grep`, but on the text with comments, strings and
character literals blanked (`source_facts.mask()`), so a macro named in a comment is not taken; arguments are read
from the original text. Which paths a catalog takes (`.c` only for syscalls, the trace headers for tracepoints...) is
decided when a revision is assembled: a blob does not know its paths. Binary files (a NUL byte in the first 8000
characters, as `git grep -I`) have no record.
"""
from __future__ import annotations

import json
import re
import zlib
from typing import Any

from parser.source_facts import mask

FAMILY = 2
KAPI_FORMAT = 1
MAX_PER_FILE = 5000
MAX_META_PER_FILE = 200
MAX_TABLES_PER_FILE = 50

# Any line that may hold one of the macros (the per-kind patterns below decide)
_CANDIDATE = re.compile(r"EXPORT_|SYSCALL_DEFINE|__setup|early_param|core_param|module_|MODULE_|TRACE_EVENT|DEFINE_EVENT"
                        r"|DECLARE_EVENT_CLASS|DECLARE_TRACE")
EXPORT_LINE = re.compile(r"\s*EXPORT_(UNUSED_)?SYMBOL(_GPL(_FUTURE)?|_NS(_GPL)?)?\s*\(")
SYSCALL_LINE = re.compile(r"(COMPAT_)?SYSCALL_DEFINE[0-6]?\(")
PARAM_LINE = re.compile(r"\b(__setup|early_param|core_param|module_param(_named|_array|_array_named|_string|_cb|_unsafe)?"
                        r"|MODULE_PARM_DESC)\s*\(")
PARAM_MACRO = re.compile(r"\b(__setup|early_param|core_param|module_param\w*|MODULE_PARM_DESC)\s*\(")
TP_LINE = re.compile(r"(TRACE_EVENT(_FN|_CONDITION)?|DEFINE_EVENT(_PRINT|_FN|_CONDITION)?|DECLARE_EVENT_CLASS|DECLARE_TRACE)\(")
META_LINE = re.compile(r"\b(MODULE_(LICENSE|AUTHOR|DESCRIPTION|ALIAS|FIRMWARE|DEVICE_TABLE|VERSION|SOFTDEP)|module_init"
                       r"|module_exit|module_[a-z0-9]+_driver)\s*\(")
META_MACRO = re.compile(r"\b(MODULE_\w+|module_init|module_exit|module_[a-z0-9]+_driver)\s*\(")
TABLE_LINE = re.compile(r"\bMODULE_DEVICE_TABLE\s*\(")
_DEFINE_LINE = re.compile(r"^\s*#\s*define\b")
_TRACE_SYSTEM = re.compile(r"^#define\s+TRACE_SYSTEM\s+(\w+)", re.M)
_QUOTED_INCLUDE = re.compile(r'^\s*#\s*include\s+"([^"]+)"', re.M)


# ------------------------------------------------------------------------------------- reading macro calls

def strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/|//[^\n]*", " ", text, flags=re.S)


def invocation(lines: list[str], line: int, start_col: int = 0, max_lines: int = 60) -> str:
    """The text of a macro invocation starting on `line` (1-based): from the first "(" to its match,
    strings and comments respected; "" when unbalanced within `max_lines`."""
    text = "\n".join(lines[line - 1: line - 1 + max_lines])
    i = text.find("(", start_col)
    if i < 0:
        return ""
    depth = 0
    j = i
    in_str = None
    while j < len(text):
        c = text[j]
        if in_str:
            if c == "\\":
                j += 2
                continue
            if c == in_str:
                in_str = None
        elif c in "\"'":
            in_str = c
        elif text.startswith("/*", j):
            k = text.find("*/", j + 2)
            j = (k + 2) if k >= 0 else len(text)
            continue
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[i + 1:j]
        j += 1
    return ""


def split_args(text: str) -> list[str]:
    """Top-level comma-separated arguments (parentheses, brackets, braces and strings respected;
    comments removed first)."""
    text = strip_comments(text)
    out, depth, cur, in_str, i = [], 0, [], None, 0
    while i < len(text):
        c = text[i]
        if in_str:
            cur.append(c)
            if c == "\\" and i + 1 < len(text):
                cur.append(text[i + 1])
                i += 2
                continue
            if c == in_str:
                in_str = None
        elif c in "\"'":
            in_str = c
            cur.append(c)
        elif c in "([{":
            depth += 1
            cur.append(c)
        elif c in ")]}":
            depth -= 1
            cur.append(c)
        elif c == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return [re.sub(r"\s+", " ", strip_comments(a)).strip() for a in out]


def string_value(arg: str) -> str | None:
    """Concatenated C string literals of an argument ("a" "b" -> ab); None when it is not one."""
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', arg)
    if not parts or re.sub(r'"(?:[^"\\]|\\.)*"', "", arg).strip():
        return None
    return "".join(parts).encode().decode("unicode_escape", "replace") if "\\" in "".join(parts) else "".join(parts)


# ------------------------------------------------------------------------------------- exports

def parse_export(text: str) -> dict[str, str] | None:
    m = re.match(r"\s*EXPORT_((?:UNUSED_)?SYMBOL(?:_GPL(?:_FUTURE)?|_NS(?:_GPL)?)?)\s*\(\s*(\w+)\s*(?:,\s*\"?([\w.]+)\"?\s*)?\)", text)
    if not m:
        return None
    macro = m.group(1)
    variant = "gpl_future" if "FUTURE" in macro else "ns_gpl" if "NS_GPL" in macro else "ns" if "_NS" in macro else "gpl" if "GPL" in macro else "unused" if "UNUSED" in macro else "plain"
    if "UNUSED" in macro:
        variant = "unused_gpl" if "GPL" in macro else "unused"
    return {"symbol": m.group(2), "variant": variant, "namespace": m.group(3) or ""}


def _export(lines: list[str], line: int) -> dict[str, str] | None:
    text = lines[line - 1]
    e = parse_export(text)
    if e is None:
        body = invocation(lines, line)
        e = parse_export(f"EXPORT_SYMBOL({body})") if body else None
        if e:
            e["variant"] = parse_export(text + "x)")["variant"] if parse_export(text + "x)") else "plain"
    return e


# ------------------------------------------------------------------------------------- tracepoints

def parse_tp_fields(entry: str) -> list[dict[str, str]]:
    fields = []
    for m in re.finditer(r"__(field|field_ext|array|string|dynamic_array|bitmask|field_struct)\s*\(", entry):
        body = invocation([entry[m.start():]], 1)
        args = split_args(body)
        if not args:
            continue
        kind = m.group(1)
        if kind == "string":
            fields.append({"kind": kind, "type": "char *", "name": args[0], "size": ""})
        elif kind == "bitmask":
            fields.append({"kind": kind, "type": "bitmask", "name": args[0], "size": args[1] if len(args) > 1 else ""})
        else:
            fields.append({"kind": kind, "type": args[0], "name": args[1] if len(args) > 1 else "",
                           "size": args[2] if len(args) > 2 else ""})
    return fields


def parse_tracepoint(macro: str, body: str) -> dict[str, Any]:
    args = split_args(body)
    out: dict[str, Any] = {"macro": macro, "name": "", "class": "", "proto": "", "args": "", "fields": [], "printk": ""}
    if macro.startswith("DEFINE_EVENT"):
        out["class"] = args[0] if args else ""
        out["name"] = args[1] if len(args) > 1 else ""
    else:
        out["name"] = args[0] if args else ""
    for a in args:
        for key, tag in (("proto", "TP_PROTO"), ("args", "TP_ARGS"), ("printk", "TP_printk")):
            if a.startswith(tag):
                out[key] = invocation([a], 1)
        if a.startswith("TP_STRUCT__entry"):
            out["fields"] = parse_tp_fields(invocation([a], 1))
    return out


# ------------------------------------------------------------------------------------- device tables

def parse_defines(text: str) -> dict[str, str]:
    """Simple `#define NAME value` constants (hex, decimal or one identifier); the first definition wins."""
    out = {}
    for m in re.finditer(r"^\s*#\s*define\s+(\w+)\s+(\(?\s*0[xX][0-9a-fA-F]+\s*\)?|\(?\s*\d+\s*\)?|\w+)\s*(?:/[/*].*)?$", text, re.M):
        out.setdefault(m.group(1), m.group(2).strip("() \t"))
    return out


def quoted_includes(text: str) -> list[str]:
    """The first 20 `#include "..."` names of a file."""
    return _QUOTED_INCLUDE.findall(text)[:20]


def table_initializer(lines: list[str], name: str) -> tuple[str, int] | None:
    """(initializer text between the braces, line) of `struct x_device_id <name>[] = {…}` or
    `DEFINE_PCI_DEVICE_TABLE(<name>) = {…}` (the form e1000e uses at v3.0)."""
    pat = re.compile(rf"(?:\bstruct\s+\w+_device_id\s+(?:__\w+\s+)*{re.escape(name)}\s*\[[^\]]*\]|DEFINE_\w+_DEVICE_TABLE\s*\(\s*{re.escape(name)}\s*\))")
    for i, line in enumerate(lines):
        if pat.search(line):
            text = "\n".join(lines[i:i + 3000])
            start = text.find("{", text.find(name))
            if start < 0:
                return None
            depth = 0
            for j in range(start, len(text)):
                c = text[j]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start + 1:j], i + 1
            return None
    return None


def entries_of(init: str) -> list[str]:
    """Top-level entries of a table initializer (each `{…}` or bare macro call), terminator dropped."""
    out = []
    for a in split_args(init):
        a = a.strip()
        if not a or re.fullmatch(r"\{\s*(?:0\s*,?\s*)*\}|\{\s*\}", a):
            continue
        out.append(a)
    return out


# ------------------------------------------------------------------------------------- extraction

def _candidate_lines(masked: str) -> list[int]:
    """1-based numbers of the lines holding a candidate macro name, in order."""
    out: list[int] = []
    line, pos = 1, 0
    for m in _CANDIDATE.finditer(masked):
        line += masked.count("\n", pos, m.start())
        pos = m.start()
        if not out or out[-1] != line:
            out.append(line)
    return out


def extract(text: str, masked: str | None = None) -> dict[str, Any]:
    """The Kernel API record of one file (see the module docstring); `masked` is `source_facts.mask(text)` when the
    caller has it already."""
    if "\0" in text[:8000]:
        return {}
    if masked is None:
        masked = mask(text)
    lines = text.split("\n")
    mlines = masked.split("\n")
    x: list[list[Any]] = []
    s: list[list[Any]] = []
    p: list[list[Any]] = []
    m: list[list[Any]] = []
    e: list[list[Any]] = []
    v: list[list[Any]] = []
    meta_lines = table_lines = 0
    for ln in _candidate_lines(masked):
        if ln > len(mlines):
            break
        code, raw = mlines[ln - 1], lines[ln - 1]
        if len(x) < MAX_PER_FILE and EXPORT_LINE.match(code):
            ex = _export(lines, ln)
            if ex:
                x.append([ex["symbol"], ex["variant"], ex["namespace"], ln])
        if len(s) < MAX_PER_FILE and SYSCALL_LINE.match(code):
            args = split_args(invocation(lines, ln))
            if args:
                s.append([raw.split("(", 1)[0].strip(), ln, args])
        if len(p) < MAX_PER_FILE and PARAM_LINE.search(code):
            macro = PARAM_MACRO.search(raw)
            if macro and (macro.group(1) == "MODULE_PARM_DESC" or not _DEFINE_LINE.match(raw)):
                body = invocation(lines, ln, start_col=0)
                p.append([macro.group(1), ln, body.strip()[:400], split_args(body)])
        if len(e) < MAX_PER_FILE and TP_LINE.match(code):
            name = raw.split("(", 1)[0]
            tp = parse_tracepoint(name, invocation(lines, ln, max_lines=200))
            if tp["name"] and re.fullmatch(r"\w+", tp["name"]):
                e.append([ln, tp])
        if meta_lines < MAX_META_PER_FILE and META_LINE.search(code):
            meta_lines += 1
            macro = META_MACRO.search(raw)
            if macro and not _DEFINE_LINE.match(raw):
                m.append([macro.group(1), ln, split_args(invocation(lines, ln))])
        if table_lines < MAX_TABLES_PER_FILE and TABLE_LINE.search(code):
            table_lines += 1
            if not _DEFINE_LINE.match(raw):
                args = split_args(invocation(lines, ln))
                if len(args) == 2 and re.fullmatch(r"\w+", args[1]):
                    found = table_initializer(lines, args[1])
                    v.append([args[0], args[1], found[1], entries_of(found[0])] if found else [args[0], args[1], 0, None])
    out: dict[str, Any] = {}
    for key, rows in (("x", x), ("s", s), ("p", p), ("m", m), ("e", e), ("v", v)):
        if rows:
            out[key] = rows
    if e:
        ts = _TRACE_SYSTEM.search(text)
        out["ts"] = ts.group(1) if ts else ""
    if v:
        out["vi"] = quoted_includes(text)
        out["vd"] = parse_defines(text)
    return out


def extract_many(items: list[tuple[str, str]]) -> list[tuple[str, dict[str, Any] | None]]:
    """[(blob sha, text)] -> [(blob sha, record)] (None when a file defeats the scanner)."""
    out: list[tuple[str, dict[str, Any] | None]] = []
    for sha, text in items:
        try:
            out.append((sha, extract(text)))
        except (RecursionError, ValueError, IndexError):
            out.append((sha, None))
    return out


def encode(record: dict[str, Any] | None) -> bytes:
    """Stored form of a record (`m_blob_facts.data` of family 2, the webapp's local store): empty for a file without
    Kernel API macros (most of them), else zlib-compressed JSON."""
    if not record:
        return b""
    return zlib.compress(json.dumps(record, separators=(",", ":")).encode(), 3)


def decode(data: bytes | bytearray | memoryview | None) -> dict[str, Any] | None:
    """`encode()` reversed (None when the bytes are not a stored record)."""
    if data is None:
        return None
    raw = bytes(data)
    if not raw:
        return {}
    try:
        record = json.loads(zlib.decompress(raw))
    except (zlib.error, ValueError):
        return None
    return record if isinstance(record, dict) else None
