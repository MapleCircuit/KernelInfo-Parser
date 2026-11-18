"""parser/kapi_catalog.py - Pure Kernel API catalog builders & summaries.

Assembles revision-level Kernel API catalogs (syscalls, exports, params, tracepoints, modules, device index)
and release summaries from per-blob `parser/kapi_facts.py` records and source files, without any database
or webapp dependencies.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import zlib
from collections import ChainMap, defaultdict
from typing import Any, Callable

from parser.kapi_facts import (
    entries_of,
    parse_defines,
    quoted_includes,
    split_args,
    string_value,
)
from parser.kbuild_conditions import (
    KbuildConditionResolver,
    M,
    N,
    Y,
    evaluate,
    is_other_arch_path,
)
from parser.kconfig_ast.kconfig_model import arch_aliases

CATALOG_FORMAT = 3

KAPI_KINDS: tuple[str, ...] = ("syscalls", "exports", "params", "tracepoints", "modules", "devices")
ARCH_INDEPENDENT_KINDS: tuple[str, ...] = ("exports", "tracepoints", "devices")
ARCH_SCOPED_KINDS: tuple[str, ...] = ("syscalls", "params", "modules")

PCI_ANY = 0xFFFFFFFF
TBL_EXCLUDED_ABIS = {"x32", "oabi", "spu", "nospu"}
_ARCH_SYNONYMS = {
    "x86": "x86_64",
    "amd64": "x86_64",
    "aarch64": "arm64",
    "riscv": "riscv64",
}


def encode_catalog(payload: dict[str, Any]) -> bytes:
    """Compress a catalog payload dict as zlib-compressed compact UTF-8 JSON."""
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return zlib.compress(raw, level=6)


def decode_catalog(data: bytes | bytearray | memoryview) -> dict[str, Any]:
    """Decompress a catalog BLOB produced by `encode_catalog()`."""
    return json.loads(zlib.decompress(bytes(data)).decode("utf-8"))


class TextCache:
    """Line-cached file reader for one revision, backed by a `read_file(path) -> bytes | str | None` callable."""

    def __init__(self, read_file: Callable[[str], bytes | str | None]) -> None:
        self._read = read_file
        self._lines: dict[str, list[str]] = {}

    def lines(self, path: str) -> list[str]:
        if path not in self._lines:
            data = self._read(path)
            if data is None:
                self._lines[path] = []
            elif isinstance(data, (bytes, bytearray, memoryview)):
                self._lines[path] = bytes(data).decode("utf-8", "replace").split("\n")
            else:
                self._lines[path] = str(data).split("\n")
        return self._lines[path]

    def text(self, path: str) -> str:
        return "\n".join(self.lines(path))


def resolve_arch_info(files: TextCache, paths: set[str], arch: str) -> dict[str, str]:
    """Resolve `arch` and `srcarch` from the top-level Makefile and `arch/` directories."""
    arch_id = (arch or "x86_64").strip().lower()
    arch_id = _ARCH_SYNONYMS.get(arch_id, arch_id)
    makefile = files.text("Makefile") if "Makefile" in paths else ""
    aliases = arch_aliases(makefile) if makefile else {}
    arch_dirs = {p.split("/", 2)[1] for p in paths if p.startswith("arch/") and p.count("/") >= 2}
    srcarch = aliases.get(arch_id, arch_id)
    if arch_dirs and srcarch not in arch_dirs and arch_id in arch_dirs:
        srcarch = arch_id
    return {"arch": arch_id, "srcarch": srcarch}


# ------------------------------------------------------------------------------- Kbuild helpers

def required_symbols(expr: Any) -> list[str]:
    """Symbols whose value N makes `expr` evaluate to N (all others Y): what must be enabled for it."""
    syms: set[str] = set()

    def walk(e: Any) -> None:
        if isinstance(e, dict):
            for k, v in e.items():
                if k in ("sym", "def", "val") and isinstance(v, str):
                    syms.add(v)
                else:
                    walk(v)
        elif isinstance(e, list):
            for x in e:
                walk(x)

    walk(expr)
    out = []
    for s in sorted(syms):
        if evaluate(expr, lambda name, s=s: N if name == s else Y) == N:
            out.append(s)
    return out


def module_capable(expr: Any) -> bool:
    """Whether `expr` can evaluate to M (a tristate symbol gates it)."""
    syms: set[str] = set()

    def walk(e: Any) -> None:
        if isinstance(e, dict):
            if "sym" in e and isinstance(e["sym"], str):
                syms.add(e["sym"])
            for v in e.values():
                walk(v)
        elif isinstance(e, list):
            for x in e:
                walk(x)

    walk(expr)
    return any(evaluate(expr, lambda name, s=s: M if name == s else Y) == M for s in syms)


def build_kbuild_index(
    files: TextCache,
    paths: set[str],
    arch: str,
    srcarch: str,
) -> dict[str, Any]:
    """Build the Kbuild object index for `(arch, srcarch)` using `KbuildConditionResolver`."""
    def _read_opt(p: str) -> str | None:
        return files.text(p) if p in paths else None

    resolver = KbuildConditionResolver(_read_opt, lambda p: p in paths, arch, srcarch)
    exprs: list[Any] = []
    intern: dict[str, int] = {}

    def ix(expr: Any) -> int:
        key = json.dumps(expr, sort_keys=True, separators=(",", ":"))
        if key not in intern:
            intern[key] = len(exprs)
            exprs.append(expr)
        return intern[key]

    objects: list[dict[str, Any]] = []
    file_map: dict[str, list[int]] = {}
    for obj in resolver.objects():
        members = [{"path": p, "expr": ix(c)} for p, c in obj.members]
        objects.append({
            "target": obj.target,
            "modname": obj.modname,
            "kind": obj.kind,
            "expr": ix(obj.condition),
            "composite": obj.composite,
            "members": members,
            "rules": [r.as_dict() for r in obj.rules],
        })
        idx = len(objects) - 1
        for m in members:
            file_map.setdefault(m["path"], []).append(idx)
    return {
        "arch": arch,
        "srcarch": srcarch,
        "exprs": exprs,
        "objects": objects,
        "files": file_map,
    }


def kbuild_owner(idx: dict[str, Any] | None, path: str) -> dict[str, Any] | None:
    """Return the primary owning Kbuild object summary for `path`, if any."""
    if not idx:
        return None
    hits = idx["files"].get(path) or []
    if not hits:
        return None
    o = idx["objects"][hits[0]]
    return {"modname": o["modname"], "target": o["target"], "expr": idx["exprs"][o["expr"]]}


# ------------------------------------------------------------------------------- A1 syscalls

def parse_syscall_tbl(text: str) -> list[dict[str, Any]]:
    """Parse `<nr> <abi> <name> [<entry> [<compat entry>]]` lines."""
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        parts = line.split()
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        out.append({
            "nr": int(parts[0]),
            "abi": parts[1],
            "name": parts[2],
            "entry": parts[3] if len(parts) > 3 else "",
            "compat_entry": parts[4] if len(parts) > 4 else "",
        })
    return out


def parse_unistd_header(text: str, bits32: bool = False) -> list[dict[str, Any]]:
    """Parse `#define __NR_<name> <n>` and `__SYSCALL`/`__SC_COMP`/`__SC_3264` macros."""
    numbers: dict[str, int] = {}
    base: dict[str, int] = {}
    alias: dict[str, str] = {}
    entries: dict[str, str] = {}
    base_entries: dict[str, str] = {}
    for m in re.finditer(
        r"^#define\s+__NR(3264)?_(\w+)\s+(?:\(?\s*(__NR(?:3264)?_\w+|\d+)\s*(?:\+\s*(\d+))?\s*\)?)\s*$",
        text,
        re.M,
    ):
        is3264, name, val, plus = m.group(1), m.group(2), m.group(3), m.group(4)
        target = base if is3264 else numbers
        if val.isdigit():
            target[name] = int(val)
            continue
        ref3264 = val.startswith("__NR3264_")
        ref = val[9:] if ref3264 else val[5:]
        src = base if ref3264 else numbers
        if ref in src:
            target[name] = src[ref] + int(plus or 0)
            if ref3264 and not is3264:
                alias[name] = ref
    for m in re.finditer(r"__(SYSCALL|SC_COMP|SC_3264|SC_COMP_3264)\(\s*__NR(3264)?_(\w+)\s*,([^\)]*)\)", text):
        args = [x.strip() for x in m.group(4).split(",")]
        pick = 1 if m.group(1).endswith("3264") and not bits32 and len(args) > 1 else 0
        (base_entries if m.group(2) else entries).setdefault(m.group(3), args[pick])
    return [
        {
            "nr": nr,
            "abi": "",
            "name": name,
            "compat_entry": "",
            "entry": entries.get(name) or base_entries.get(alias.get(name, "")) or f"sys_{name}",
        }
        for name, nr in sorted(numbers.items(), key=lambda kv: kv[1])
        if not name.startswith("syscall")
        and not name.endswith("_syscalls")
        and name == name.lower()
        and name not in ("arch_specific_syscall",)
    ]


_PP_TOKEN = re.compile(r"defined\s*\(\s*\w+\s*\)|defined\s+\w+|\|\||&&|==|!=|>=|<=|[!()<>]|\w+")


def _pp_eval(expr: str, bits32: bool, defines: dict[str, int] | None = None) -> bool | None:
    known = {"__BITS_PER_LONG": 32 if bits32 else 64, "BITS_PER_LONG": 32 if bits32 else 64, **(defines or {})}
    toks = _PP_TOKEN.findall(expr.split("/*", 1)[0].split("//", 1)[0])
    pos = 0

    def peek() -> str:
        return toks[pos] if pos < len(toks) else ""

    def take() -> str:
        nonlocal pos
        pos += 1
        return toks[pos - 1]

    def atom() -> Any:
        t = take() if peek() else ""
        if t == "(":
            v = orr()
            if peek() == ")":
                take()
            return v
        if t == "!":
            v = atom()
            return None if v is None else not v
        if t.startswith("defined"):
            name = re.sub(r"defined|[\s()]", "", t)
            return True if name in known else False if name == "__SYSCALL_COMPAT" else None
        if t.isdigit():
            return int(t)
        return known.get(t)

    def cmp() -> bool | None:
        v = atom()
        if peek() in ("==", "!=", ">=", "<=", ">", "<"):
            op, w = take(), atom()
            if v is None or w is None or isinstance(v, bool) or isinstance(w, bool):
                return None
            return {"==": v == w, "!=": v != w, ">=": v >= w, "<=": v <= w, ">": v > w, "<": v < w}[op]
        return v if v is None or isinstance(v, bool) else bool(v)

    def andd() -> bool | None:
        v = cmp()
        while peek() == "&&":
            take()
            w = cmp()
            v = False if v is False or w is False else (None if v is None or w is None else True)
        return v

    def orr() -> bool | None:
        v = andd()
        while peek() == "||":
            take()
            w = andd()
            v = True if v is True or w is True else (None if v is None or w is None else False)
        return v

    try:
        return orr()
    except (IndexError, KeyError, TypeError):
        return None


def bits_conditional(text: str, bits32: bool, defines: dict[str, int] | None = None) -> str:
    """Filter out preprocessor branches that cannot hold for the target word size."""
    out: list[str] = []
    stack: list[list[bool | None]] = []
    for line in re.sub(r"\\\n", " ", text).split("\n"):
        d = re.match(r"^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)$", line)
        if not d:
            if all(f[0] is not False for f in stack):
                out.append(line)
            continue
        kind, rest = d.group(1), d.group(2)
        if kind == "if":
            v = _pp_eval(rest, bits32, defines)
            stack.append([v, v])
        elif kind in ("ifdef", "ifndef"):
            v = _pp_eval(f"{'!' if kind == 'ifndef' else ''}defined({rest.strip().split(' ', 1)[0]})", bits32, defines)
            stack.append([v, v])
        elif not stack:
            continue
        elif kind == "elif":
            held = stack[-1][1]
            v = False if held is True else _pp_eval(rest, bits32, defines)
            stack[-1] = [
                v if held is False else (False if v is False else None) if held is None else v,
                True if held is True or v is True else (None if held is None or v is None else False),
            ]
        elif kind == "else":
            held = stack[-1][1]
            stack[-1] = [None if held is None else not held, True]
        else:
            stack.pop()
    return "\n".join(out)


def parse_syscall_table_s(text: str) -> list[str]:
    """Extract `.long sys_xxx` entries in order (index is syscall number)."""
    return re.findall(r"^\s*\.long\s+(\w+)", text, re.M)


def _is_32bit(arch: str) -> bool:
    return arch in ("i386", "x86", "mips") or arch.endswith("32")


def _syscall_sources(
    files: TextCache,
    arch: str,
    srcarch: str,
    paths: set[str],
    bits32: bool | None = None,
) -> list[tuple[str, str]]:
    bits32 = _is_32bit(arch) if bits32 is None else bits32
    if srcarch == "x86":
        name = "syscall_32.tbl" if bits32 else "syscall_64.tbl"
        for d in ("arch/x86/entry/syscalls", "arch/x86/syscalls"):
            if f"{d}/{name}" in paths:
                return [("tbl", f"{d}/{name}")]
        if bits32 and "arch/x86/kernel/syscall_table_32.S" in paths:
            return [("table_S", "arch/x86/kernel/syscall_table_32.S"), ("header", "arch/x86/include/asm/unistd_32.h")]
        return [("header", "arch/x86/include/asm/unistd_32.h" if bits32 else "arch/x86/include/asm/unistd_64.h")]
    makefile = f"arch/{srcarch}/kernel/Makefile.syscalls"
    if makefile in paths:
        m = re.search(r"^syscalltbl\s*:?=\s*(\S+)", files.text(makefile), re.M)
        table = m.group(1).replace("$(srctree)/", "").replace("%", "32" if bits32 else "64") if m else "scripts/syscall.tbl"
        return [("tbl", table), ("abis", makefile)]
    cands = [
        f"arch/{srcarch}/kernel/syscalls/syscall.tbl",
        f"arch/{srcarch}/tools/syscall.tbl",
        f"arch/{srcarch}/tools/syscall_{'32' if bits32 else '64'}.tbl",
        f"arch/{srcarch}/kernel/syscalls/syscall_{'o32' if bits32 else 'n64'}.tbl",
    ]
    for c in cands:
        if c in paths:
            return [("tbl", c)]
    for c in (
        f"arch/{srcarch}/include/uapi/asm/unistd.h",
        f"arch/{srcarch}/include/asm/unistd.h",
        "include/uapi/asm-generic/unistd.h",
        "include/asm-generic/unistd.h",
        "scripts/syscall.tbl",
    ):
        if c in paths:
            return [("tbl" if c.endswith(".tbl") else "header", c)]
    return []


_UNISTD_INCLUDE = re.compile(r'^\s*#\s*include\s+([<"])([^>"]*unistd[^>"]*)[>"]', re.M)


def _header_text(
    files: TextCache,
    paths: set[str],
    path: str,
    srcarch: str,
    bits32: bool,
    seen: set[str] | None = None,
) -> str:
    seen = seen if seen is not None else set()
    seen.add(path)
    mips = {"_MIPS_SIM_ABI32": 1, "_MIPS_SIM_NABI32": 2, "_MIPS_SIM_ABI64": 3, "_MIPS_SIM": 1 if bits32 else 3}
    text = bits_conditional(files.text(path), bits32, mips if srcarch == "mips" else None)
    parts = [text]
    for m in _UNISTD_INCLUDE.finditer(text):
        inc = m.group(2)
        if re.search(r"_64\b" if bits32 else r"_32\b", inc.rsplit("/", 1)[-1].removesuffix(".h")):
            continue
        if m.group(1) == '"':
            cands = [os.path.normpath(os.path.join(os.path.dirname(path), inc))]
        elif inc.startswith("asm-generic/"):
            cands = [f"include/uapi/{inc}", f"include/{inc}"]
        elif inc.startswith("asm/"):
            cands = [f"arch/{srcarch}/include/uapi/{inc}", f"arch/{srcarch}/include/{inc}"]
        elif inc.startswith("uapi/"):
            cands = [f"arch/{srcarch}/include/{inc}"]
        elif inc.startswith("arch/"):
            cands = sorted(p for p in paths if p.startswith(f"arch/{srcarch}/include/arch-") and p.endswith(f"/{inc}"))[:1]
        else:
            cands = []
        for c in cands:
            if c in paths and c not in seen:
                parts.append(_header_text(files, paths, c, srcarch, bits32, seen))
                break
    return "\n".join(parts)


def _table_text(files: TextCache, paths: set[str], path: str) -> str:
    lines = [ln for ln in files.lines(path) if ln.strip()]
    if len(lines) == 1 and lines[0].endswith(".tbl") and not lines[0][:1].isdigit():
        target = os.path.normpath(os.path.join(os.path.dirname(path), lines[0].strip()))
        if target in paths:
            return files.text(target)
    return "\n".join(lines)


def _syscall_rows(
    files: TextCache,
    paths: set[str],
    sources: list[tuple[str, str]],
    srcarch: str,
    bits32: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    table_s = None
    for kind, path in sources:
        if kind == "tbl":
            rows = [r for r in parse_syscall_tbl(_table_text(files, paths, path)) if r["abi"] not in TBL_EXCLUDED_ABIS]
        elif kind == "abis":
            words = [f"common,{'32' if bits32 else '64'}"]
            for m in re.finditer(r"^syscall_abis_(32|64)\s*\+?=\s*(.*)$", files.text(path), re.M):
                if m.group(1) == ("32" if bits32 else "64"):
                    words.append(m.group(2))
            abis = set(re.split(r"[\s,]+", " ".join(words))) - {""}
            rows = [r for r in rows if r["abi"] in abis]
        elif kind == "table_S":
            table_s = parse_syscall_table_s(files.text(path))
        else:
            rows = parse_unistd_header(_header_text(files, paths, path, srcarch, bits32), bits32)
    if table_s is not None:
        by_nr = {r["nr"]: r for r in rows}
        rows = [
            {
                "nr": i,
                "abi": "i386",
                "name": by_nr[i]["name"] if i in by_nr else e.removeprefix("sys_"),
                "entry": e,
                "compat_entry": "",
            }
            for i, e in enumerate(table_s)
        ]
    return rows


def _entry_key(r: dict[str, Any]) -> str:
    key = re.sub(r"^(?:__x64_|__ia32_|__se_|ptregs_|stub_|sys32_)", "", r["entry"] or f"sys_{r['name']}")
    return key if key.startswith(("sys_", "compat_sys_")) else "sys_" + key


def _is_ni(r: dict[str, Any]) -> bool:
    return r["entry"] in ("sys_ni_syscall", "ni_syscall") or r["name"] == "ni_syscall"


def _numbered_elsewhere(files: TextCache, paths: set[str], arch: str, srcarch: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for sa in sorted({p.split("/", 2)[1] for p in paths if p.startswith("arch/") and p.count("/") >= 2}):
        if sa == "x86":
            variants = [("i386", "i386", True), ("x86_64", "x86_64", False)]
        elif sa.endswith("64"):
            variants = [(sa, sa, False)]
        else:
            variants = [(sa, sa, True), (sa, f"{sa}64", False)]
        for label, arch_name, bits32 in variants:
            if sa == srcarch and (sa != "x86" or bits32 == _is_32bit(arch)):
                continue
            srcs = _syscall_sources(files, arch_name, sa, paths, bits32)
            if not any(p.startswith(f"arch/{sa}/") for _k, p in srcs):
                continue
            for r in _syscall_rows(files, paths, srcs, sa, bits32):
                if not _is_ni(r):
                    out[_entry_key(r)].add(label)
                    out[f"sys_{r['name']}"].add(label)
    return out


def _syscall_defs(records: list[tuple[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path, rec in records:
        if not path.endswith(".c"):
            continue
        for macro, line, args in rec.get("s", ()):
            compat = macro.startswith("COMPAT")
            name = args[0]
            params = [{"type": args[i], "name": args[i + 1]} for i in range(1, len(args) - 1, 2)]
            entry = f"{'compat_sys' if compat else 'sys'}_{name}"
            out.setdefault(entry, {"path": path, "line_s": line, "macro": macro, "args": params})
    return out


def build_syscalls(
    files: TextCache,
    paths: set[str],
    records: list[tuple[str, dict[str, Any]]],
    arch: str,
    srcarch: str,
) -> dict[str, Any]:
    """Assemble the syscall catalog for `(arch, srcarch)`."""
    sources = _syscall_sources(files, arch, srcarch, paths)
    rows = _syscall_rows(files, paths, sources, srcarch, _is_32bit(arch))
    defs = _syscall_defs(records)
    used: set[str] = set()
    for r in rows:
        key = _entry_key(r)
        d = defs.get(key) or defs.get(f"sys_{r['name']}")
        if _is_ni(r):
            r["status"] = "ni_syscall"
        elif d:
            r["status"] = "defined"
            used.add(key if key in defs else f"sys_{r['name']}")
        else:
            r["status"] = "no_definition"
        r["definition"] = d
    elsewhere = _numbered_elsewhere(files, paths, arch, srcarch)
    unnumbered = [
        {"name": k.removeprefix("sys_"), **v, "numbered_on": sorted(elsewhere.get(k, ()))}
        for k, v in sorted(defs.items())
        if k.startswith("sys_") and k not in used
    ]
    return {
        "arch": arch,
        "srcarch": srcarch,
        "sources": [{"kind": k, "path": p} for k, p in sources],
        "syscalls": rows,
        "unnumbered": unnumbered,
    }


# ------------------------------------------------------------------------------- A2 exports

def build_exports(records: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Assemble the exported symbols catalog."""
    out = []
    for path, rec in records:
        for symbol, variant, namespace, line in rec.get("x", ()):
            out.append({"symbol": symbol, "variant": variant, "namespace": namespace, "path": path, "line": line})
    counts: dict[str, int] = defaultdict(int)
    for e in out:
        counts[e["variant"]] += 1
    return {"exports": out, "total": len(out), "counts": dict(counts)}


# ------------------------------------------------------------------------------- A3 parameters

def parse_kernel_parameters_doc(text: str) -> dict[str, dict[str, Any]]:
    """Parse `Documentation/(admin-guide/)kernel-parameters.txt` entries."""
    out: dict[str, dict[str, Any]] = {}
    current = None
    for i, raw in enumerate(text.splitlines(), 1):
        m = re.match(r"^\t([A-Za-z0-9_.\-/]+[A-Za-z0-9_\-/])(=?)(?:\s*\[([^\]]*)\])?(.*)$", raw)
        if m and not raw.startswith("\t\t"):
            name = m.group(1)
            current = out.setdefault(
                name,
                {
                    "name": name,
                    "tags": [t.strip() for t in (m.group(3) or "").split(",") if t.strip()],
                    "text": m.group(4).strip(),
                    "line": i,
                    "takes_value": bool(m.group(2)),
                },
            )
            continue
        if current and raw.startswith("\t\t"):
            if len(current["text"]) < 3000:
                current["text"] = (current["text"] + "\n" + raw.strip()).strip()
        elif raw.strip() == "":
            continue
        else:
            current = None
    return out


def _param_row(name: str, path: str, line: int, body: str, args: list[str]) -> dict[str, Any] | None:
    row: dict[str, Any] = {"macro": name, "path": path, "line": line, "snippet": f"{name}({body})"}
    if name in ("__setup", "early_param"):
        lit = string_value(args[0]) if args else None
        if lit is None:
            return None
        row.update(
            kind="setup" if name == "__setup" else "early",
            name=lit.rstrip("="),
            takes_value=lit.endswith("="),
            handler=args[1] if len(args) > 1 else "",
            type="",
            perm="",
        )
    elif name == "core_param":
        row.update(
            kind="core",
            name=args[0] if args else "",
            handler="",
            type=args[2] if len(args) > 2 else "",
            perm=args[3] if len(args) > 3 else "",
            takes_value=True,
        )
    else:
        named = name in ("module_param_named", "module_param_array_named", "module_param_cb", "module_param_string")
        row.update(
            kind="module",
            name=args[0] if args else "",
            handler="",
            type=(args[2] if named and len(args) > 2 else args[1] if len(args) > 1 else ""),
            perm=args[-1] if args else "",
            takes_value=True,
        )
    return row if row.get("name") else None


def build_params(
    files: TextCache,
    paths: set[str],
    records: list[tuple[str, dict[str, Any]]],
    srcarch: str,
    kbuild_idx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the boot & module parameters catalog for `srcarch`."""
    rows: list[dict[str, Any]] = []
    descs: dict[tuple[str, str], str] = {}
    for path, rec in records:
        if (
            not rec.get("p")
            or not path.endswith((".c", ".h"))
            or path.startswith(("tools/", "scripts/"))
            or is_other_arch_path(path, srcarch)
        ):
            continue
        for name, line, body, args in rec["p"]:
            if name == "MODULE_PARM_DESC":
                if len(args) >= 2:
                    descs[(path, args[0])] = string_value(args[1]) or args[1]
                continue
            row = _param_row(name, path, line, body, args)
            if row is not None:
                rows.append(row)
    for r in rows:
        if r["kind"] == "module":
            owner = kbuild_owner(kbuild_idx, r["path"])
            r["module"] = owner["modname"] if owner else os.path.splitext(os.path.basename(r["path"]))[0].replace("-", "_")
            r["full_name"] = f"{r['module']}.{r['name']}"
            r["description"] = descs.get((r["path"], r["name"]), "")
        else:
            r["full_name"] = r["name"]
    doc_path = ""
    for p in ("Documentation/admin-guide/kernel-parameters.txt", "Documentation/kernel-parameters.txt"):
        if p in paths:
            doc_path = p
            break
    docs = parse_kernel_parameters_doc(files.text(doc_path)) if doc_path else {}
    defined: set[str] = set()
    for r in rows:
        entry = docs.get(r["full_name"]) or (docs.get(r["name"]) if r["kind"] != "module" else None)
        r["doc"] = {"line": entry["line"], "tags": entry["tags"]} if entry else None
        r["undocumented"] = r["kind"] != "module" and entry is None
        defined.add(r["full_name"])
        defined.add(r["name"])
    stale = [{"name": d["name"], "line": d["line"], "tags": d["tags"]} for d in docs.values() if d["name"] not in defined]
    return {"doc_path": doc_path, "params": rows, "stale_docs": stale, "total": len(rows)}


# ------------------------------------------------------------------------------- A4 tracepoints

def is_trace_header(path: str) -> bool:
    """Whether `path` is a tracepoint header (`include/trace/events/*` or `*trace*.h`)."""
    return path.startswith("include/trace/events/") or fnmatch.fnmatchcase(os.path.basename(path), "*trace*.h")


def build_tracepoints(records: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Assemble the tracepoints catalog with `DECLARE_EVENT_CLASS` inheritance resolved."""
    events: list[dict[str, Any]] = []
    for path, rec in records:
        if not rec.get("e") or not is_trace_header(path):
            continue
        system = rec.get("ts") or os.path.splitext(os.path.basename(path))[0]
        for line, tp in rec["e"]:
            macro = tp["macro"]
            kind = "class" if macro == "DECLARE_EVENT_CLASS" else "raw" if macro == "DECLARE_TRACE" else "event"
            events.append({**tp, "kind": kind, "system": system, "path": path, "line": line})
    classes = {e["name"]: e for e in events if e["kind"] == "class"}
    for e in events:
        if e["class"] and e["class"] in classes:
            c = classes[e["class"]]
            for key in ("proto", "args", "fields", "printk"):
                if not e[key]:
                    e[key] = c[key]
    return {"tracepoints": events, "total": len(events)}


# ------------------------------------------------------------------------------- A5 modules

def build_modules(
    records: list[tuple[str, dict[str, Any]]],
    kbuild_idx: dict[str, Any],
    arch: str | None = None,
) -> dict[str, Any]:
    """Assemble the loadable modules catalog from `kbuild_idx` and `records`."""
    meta: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path, rec in records:
        if not rec.get("m") or path not in kbuild_idx["files"] or not path.endswith((".c", ".h")):
            continue
        for macro, line, args in rec["m"]:
            meta[path].append({"macro": macro, "args": args, "path": path, "line": line})
    options: dict[int, list[str]] = {}
    modules: list[dict[str, Any]] = []
    for o in kbuild_idx["objects"]:
        expr = kbuild_idx["exprs"][o["expr"]]
        if o["kind"] != "obj" or not module_capable(expr):
            continue
        md: dict[str, Any] = {
            "license": "",
            "authors": [],
            "description": "",
            "aliases": [],
            "firmware": [],
            "device_tables": [],
            "init": "",
            "exit": "",
        }
        for mem in o["members"]:
            for e in meta.get(mem["path"], []):
                a, mac = e["args"], e["macro"]
                val = (string_value(a[0]) if a else None) or (a[0] if a else "")
                if mac == "MODULE_LICENSE":
                    md["license"] = val
                elif mac == "MODULE_AUTHOR":
                    md["authors"].append(val)
                elif mac == "MODULE_DESCRIPTION":
                    md["description"] = val
                elif mac == "MODULE_ALIAS":
                    md["aliases"].append(val)
                elif mac == "MODULE_FIRMWARE":
                    md["firmware"].append(val)
                elif mac == "MODULE_DEVICE_TABLE" and len(a) >= 2:
                    md["device_tables"].append({"bus": a[0], "table": a[1], "path": e["path"], "line": e["line"]})
                elif mac == "module_init":
                    md["init"] = val
                elif mac == "module_exit":
                    md["exit"] = val
                elif mac.startswith("module_") and mac.endswith("_driver") and not md["init"]:
                    md["init"] = f"{mac}({val})"
        if o["expr"] not in options:
            options[o["expr"]] = required_symbols(expr)
        modules.append({
            "modname": o["modname"],
            "target": o["target"],
            "expr": expr,
            "options": list(options[o["expr"]]),
            "members": [m["path"] for m in o["members"]],
            **md,
        })
    return {"arch": arch or kbuild_idx["arch"], "modules": modules, "total": len(modules)}


# ------------------------------------------------------------------------------- N4 devices

def to_int(token: str, defines: Any, depth: int = 0) -> int | None:
    """Resolve a C integer literal, `PCI_ANY_ID`, shift expression, or `#define` name."""
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


def parse_entry(bus: str, entry: str, defines: Any) -> dict[str, Any]:
    """Parse one `MODULE_DEVICE_TABLE(<bus>, <table>)` initializer entry."""
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
            rec.update(
                vendor=to_int(named.get("vendor", ""), defines),
                device=to_int(named.get("device", ""), defines),
                cls=to_int(named.get("class", ""), defines),
                mask=to_int(named.get("class_mask", ""), defines),
            )
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
            rec.update(
                vendor=to_int(named.get("idVendor", ""), defines),
                device=to_int(named.get("idProduct", ""), defines),
                cls=to_int(named.get("bInterfaceClass", ""), defines),
            )
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


def build_device_index(
    files: TextCache,
    records: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    """Assemble the `MODULE_DEVICE_TABLE` index from `records` and included headers."""
    headers: dict[str, tuple[str, list[str], dict[str, str]]] = {}

    def header(path: str) -> tuple[str, list[str], dict[str, str]]:
        if path not in headers:
            text = files.text(path)
            headers[path] = (text, quoted_includes(text), parse_defines(text)) if text else ("", [], {})
        return headers[path]

    pci_ids = header("include/linux/pci_ids.h")[2]
    dev_records = [(path, rec) for path, rec in records if rec.get("v") and path.endswith(".c")]
    tables = 0
    entries: list[dict[str, Any]] = []
    for path, rec in dev_records:
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


# ------------------------------------------------------------------------------- Summaries

def _subsystem_of(path: str) -> str:
    """Coarse subsystem bucket for a kernel source path (e.g. `drivers/net`, `fs`, `kernel`)."""
    if not path:
        return "other"
    parts = path.split("/")
    if len(parts) >= 2 and parts[0] in ("drivers", "arch", "sound", "net", "tools", "Documentation"):
        return f"{parts[0]}/{parts[1]}"
    return parts[0]


def summarize_catalog(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Compute a compact summary dict for one KAPI catalog `kind`."""
    if kind == "syscalls":
        rows = payload.get("syscalls") or []
        unnumbered = payload.get("unnumbered") or []
        by_status: dict[str, int] = defaultdict(int)
        for r in rows:
            by_status[str(r.get("status") or "unknown")] += 1
        return {
            "total": len(rows),
            "unnumbered": len(unnumbered),
            "by_status": dict(sorted(by_status.items())),
        }
    if kind == "exports":
        rows = payload.get("exports") or []
        counts = payload.get("counts") or {}
        if not counts and rows:
            tmp: dict[str, int] = defaultdict(int)
            for r in rows:
                tmp[str(r.get("variant") or "")] += 1
            counts = dict(tmp)
        return {
            "total": int(payload.get("total", len(rows))),
            "unique_symbols": len({r.get("symbol") for r in rows if r.get("symbol")}),
            "by_variant": dict(sorted(counts.items())),
        }
    if kind == "params":
        rows = payload.get("params") or []
        by_kind: dict[str, int] = defaultdict(int)
        undoc = 0
        for r in rows:
            by_kind[str(r.get("kind") or "unknown")] += 1
            if r.get("undocumented"):
                undoc += 1
        return {
            "total": int(payload.get("total", len(rows))),
            "undocumented": undoc,
            "stale_docs": len(payload.get("stale_docs") or []),
            "by_kind": dict(sorted(by_kind.items())),
        }
    if kind == "tracepoints":
        rows = payload.get("tracepoints") or []
        by_kind: dict[str, int] = defaultdict(int)
        systems: set[str] = set()
        for r in rows:
            by_kind[str(r.get("kind") or "event")] += 1
            if r.get("system"):
                systems.add(str(r["system"]))
        return {
            "total": int(payload.get("total", len(rows))),
            "systems": len(systems),
            "by_kind": dict(sorted(by_kind.items())),
        }
    if kind == "modules":
        rows = payload.get("modules") or []
        by_license: dict[str, int] = defaultdict(int)
        for r in rows:
            lic = str(r.get("license") or "unspecified")
            by_license[lic] += 1
        return {
            "total": int(payload.get("total", len(rows))),
            "by_license": dict(sorted(by_license.items(), key=lambda kv: (-kv[1], kv[0]))[:25]),
        }
    if kind == "devices":
        rows = payload.get("entries") or []
        by_bus: dict[str, int] = defaultdict(int)
        for r in rows:
            by_bus[str(r.get("bus") or "unknown")] += 1
        return {
            "total": len(rows),
            "tables": int(payload.get("tables", 0)),
            "by_bus": dict(sorted(by_bus.items())),
        }
    return {"total": 0}


def build_release_summary(catalogs_by_kind: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-release counts, per-kind summaries, and subsystem breakdown across all 6 KAPI kinds."""
    counts: dict[str, int] = {}
    kind_summaries: dict[str, dict[str, Any]] = {}
    subsystems: dict[str, dict[str, int]] = defaultdict(
        lambda: {"syscalls": 0, "exports": 0, "params": 0, "tracepoints": 0, "modules": 0, "devices": 0}
    )

    for kind in KAPI_KINDS:
        payload = catalogs_by_kind.get(kind) or {}
        summary = summarize_catalog(kind, payload)
        kind_summaries[kind] = summary
        counts[kind] = int(summary.get("total", 0))

    for r in (catalogs_by_kind.get("syscalls") or {}).get("syscalls") or []:
        d = r.get("definition")
        if d and d.get("path"):
            subsystems[_subsystem_of(d["path"])]["syscalls"] += 1

    for r in (catalogs_by_kind.get("exports") or {}).get("exports") or []:
        if r.get("path"):
            subsystems[_subsystem_of(r["path"])]["exports"] += 1

    for r in (catalogs_by_kind.get("params") or {}).get("params") or []:
        if r.get("path"):
            subsystems[_subsystem_of(r["path"])]["params"] += 1

    for r in (catalogs_by_kind.get("tracepoints") or {}).get("tracepoints") or []:
        if r.get("path"):
            subsystems[_subsystem_of(r["path"])]["tracepoints"] += 1

    for r in (catalogs_by_kind.get("modules") or {}).get("modules") or []:
        members = r.get("members") or []
        path = members[0] if members else r.get("target") or ""
        if path:
            subsystems[_subsystem_of(path)]["modules"] += 1

    for r in (catalogs_by_kind.get("devices") or {}).get("entries") or []:
        if r.get("path"):
            subsystems[_subsystem_of(r["path"])]["devices"] += 1

    ranked_subsystems = dict(
        sorted(
            subsystems.items(),
            key=lambda kv: (-sum(kv[1].values()), kv[0]),
        )
    )
    return {
        "syscalls_count": counts["syscalls"],
        "exports_count": counts["exports"],
        "params_count": counts["params"],
        "tracepoints_count": counts["tracepoints"],
        "modules_count": counts["modules"],
        "devices_count": counts["devices"],
        "kinds": kind_summaries,
        "subsystems": ranked_subsystems,
    }
