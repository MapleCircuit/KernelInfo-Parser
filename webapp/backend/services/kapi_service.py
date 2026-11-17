"""webapp/backend/services/kapi_service.py - Kernel API catalogs (A1-A5, webapp/API.md section 4.9).

Syscalls, exported symbols, boot and module parameters, tracepoints and modules of any revision, assembled from the
Kernel API records of its blobs (`parser/kapi_facts.py`): the parser stores them per Git blob SHA for the versions it
ingests (`m_blob_facts` family 2, Step 6.9), and the blobs the database lacks (git-only revisions) are extracted here
once, into a local store (`kapi_store`), so a revision is never searched as a whole. The few files read whole (syscall
tables, kernel-parameters.txt) go through the persistent Git readers; module ownership comes from the Kbuild object
index (section 4.4). Each catalog is built once per (commit, ARCH) as a background job (section 2.15) and cached on
disk (section 2.14): a commit never changes.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import re
import threading
import time
from collections import OrderedDict, defaultdict
from typing import Any, Callable

from fastapi import HTTPException

from cache_engine import CacheError
from parser import kapi_facts as kf
from parser.kapi_facts import (  # noqa: F401 - re-exported: the catalogs' readers (tests, device_index_service)
    invocation,
    parse_export,
    parse_tp_fields,
    parse_tracepoint,
    split_args,
    string_value,
    strip_comments,
)
from webapp.backend.config import get_active_repo_name
from webapp.backend.services import source_index as si
from webapp.backend.services.disk_cache import DiskCache
from webapp.backend.services.git_reader import git_reader
from webapp.backend.services.grep_service import grep_service
from webapp.backend.services.jobs import jobs

logger = logging.getLogger(__name__)

CATALOG_FORMAT = 3
_DISK = DiskCache("kapi", max_bytes=512 << 20)
SUFFIXES = (".c", ".h", ".S", ".rs")
MAX_REVISIONS = 2

# ------------------------------------------------------------------------------- source reading


class FileCache:
    """Lines of files of one commit, read once per catalog build."""

    def __init__(self, sha: str) -> None:
        self.sha = sha
        self._lines: dict[str, list[str]] = {}

    def lines(self, path: str) -> list[str]:
        if path not in self._lines:
            data, is_dir, _ = git_reader.read_file(self.sha, path)
            self._lines[path] = [] if data is None or is_dir else data.decode("utf-8", "replace").split("\n")
        return self._lines[path]


# ------------------------------------------------------------------------------- Kernel API records of a revision

kapi_store = si.FactStore("kapi", kf.KAPI_FORMAT, kf.encode, kf.decode)
_REVISIONS: OrderedDict[str, list[tuple[str, dict[str, Any]]]] = OrderedDict()
_REV_LOCK = threading.Lock()
_BUILD_LOCKS: dict[str, threading.Lock] = {}


def _load_records(sha: str, progress: Callable | None) -> list[tuple[str, dict[str, Any]]]:
    started = time.time()
    if progress:
        progress(0, 0, "listing the tree")
    files = si.revision_files(sha, SUFFIXES)
    blobs = list(dict.fromkeys(b for _p, b in files))
    records: dict[str, dict[str, Any]] = si.db_facts(blobs, kf.FAMILY, kf.KAPI_FORMAT, kf.decode)
    from_db = len(records)
    rest = [b for b in blobs if b not in records]
    if rest:
        records.update(kapi_store.get_many(rest))
    missing = [b for b in rest if b not in records]
    if missing:
        settings = si._settings()
        si.extract_blobs(missing, progress, settings["workers"], store=kapi_store, extractor=kf.extract_many, empty={})
        records.update(kapi_store.get_many(missing))
        try:
            kapi_store.prune(settings["max_mb"] << 20)
        except CacheError as exc:
            logger.debug("Kernel API store prune failed: %s", exc)
    out = [(path, records[b]) for path, b in files if records.get(b)]
    logger.info("Kernel API records of %s: %d of %d files (%d blobs from the database, %d extracted) in %.1f s",
                sha[:12], len(out), len(files), from_db, len(missing), time.time() - started)
    return out


def revision_records(sha: str, progress: Callable | None = None) -> list[tuple[str, dict[str, Any]]]:
    """(path, Kernel API record) of the `.c` / `.h` / `.S` files of a commit that have one, in tree order (the order
    `git grep` reported them in): the database's records, then the local store's, extracting the blobs neither has.
    The two most recently used revisions stay in memory."""
    with _REV_LOCK:
        hit = _REVISIONS.get(sha)
        if hit is not None:
            _REVISIONS.move_to_end(sha)
            return hit
        lock = _BUILD_LOCKS.setdefault(sha, threading.Lock())
    with lock:  # one build per commit, however many catalogs ask at once
        with _REV_LOCK:
            hit = _REVISIONS.get(sha)
        if hit is None:
            hit = _load_records(sha, progress)
            with _REV_LOCK:
                _REVISIONS[sha] = hit
                while len(_REVISIONS) > MAX_REVISIONS:
                    _REVISIONS.popitem(last=False)
    with _REV_LOCK:
        _BUILD_LOCKS.pop(sha, None)
    return hit


def resolve(version: str) -> str:
    from webapp.backend.services.release_service import resolve_sha
    from webapp.backend.security.git import safe_rev
    safe_rev(version)
    sha = resolve_sha(version)
    if not sha:
        raise HTTPException(status_code=404, detail=f"Unknown revision: {version}")
    return sha


def _catalog(kind: str, version: str, build: Callable[[str, Callable], Any], arch: str = "", wait: float = 0.0) -> dict[str, Any]:
    sha = resolve(version)
    ck = f"{get_active_repo_name()}|{kind}|{CATALOG_FORMAT}.{kf.KAPI_FORMAT}|{sha}|{arch}"
    state = jobs.submit(f"kapi:{ck}", lambda p: build(sha, p), cache=_DISK, cache_key=ck)
    if state["state"] == "building" and wait > 0:
        state = jobs.wait(f"kapi:{ck}", wait) or state
    return state


def _kbuild_files(version: str, arch: str) -> dict[str, Any] | None:
    """The Kbuild object index of the ARCH (None when it cannot be built)."""
    try:
        from webapp.backend.services.kbuild_index_service import index_now
        return index_now(version, arch, timeout=180.0)
    except Exception as exc:  # noqa: BLE001 - catalogs work without module ownership
        logger.debug("Kbuild index unavailable for %s %s: %s", version, arch, exc)
        return None


def _owner(idx: dict[str, Any] | None, path: str) -> dict[str, Any] | None:
    if not idx:
        return None
    hits = idx["files"].get(path) or []
    if not hits:
        return None
    o = idx["objects"][hits[0]]
    return {"modname": o["modname"], "target": o["target"], "expr": idx["exprs"][o["expr"]]}


# ------------------------------------------------------------------------------- A1 syscalls

TBL_EXCLUDED_ABIS = {"x32", "oabi", "spu", "nospu"}


def parse_syscall_tbl(text: str) -> list[dict[str, Any]]:
    """`<nr> <abi> <name> [<entry> [<compat entry>]]` lines."""
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        parts = line.split()
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        out.append({"nr": int(parts[0]), "abi": parts[1], "name": parts[2],
                    "entry": parts[3] if len(parts) > 3 else "", "compat_entry": parts[4] if len(parts) > 4 else ""})
    return out


def parse_unistd_header(text: str, bits32: bool = False) -> list[dict[str, Any]]:
    """`#define __NR_<name> <n>` (+ `__SYSCALL(__NR_<name>, <entry>)` / `__SC_COMP(nr, entry, compat)` /
    `__SC_3264(nr, entry32, entry64)` / `__SC_COMP_3264(nr, entry32, entry64, compat)`). asm-generic's
    `__NR3264_<name>` numbers are no rows of their own: `#define __NR_mmap __NR3264_mmap` takes its number and
    entry."""
    numbers: dict[str, int] = {}
    base: dict[str, int] = {}
    alias: dict[str, str] = {}
    entries: dict[str, str] = {}
    base_entries: dict[str, str] = {}
    for m in re.finditer(r"^#define\s+__NR(3264)?_(\w+)\s+(?:\(?\s*(__NR(?:3264)?_\w+|\d+)\s*(?:\+\s*(\d+))?\s*\)?)\s*$", text, re.M):
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
    return [{"nr": nr, "abi": "", "name": name, "compat_entry": "",
             "entry": entries.get(name) or base_entries.get(alias.get(name, "")) or f"sys_{name}"}
            for name, nr in sorted(numbers.items(), key=lambda kv: kv[1])
            if not name.startswith("syscall") and not name.endswith("_syscalls") and name == name.lower()
            and name not in ("arch_specific_syscall",)]  # no counts / bases (`__NR_Linux`, `__NR_SYSCALL_BASE`)


_PP_TOKEN = re.compile(r"defined\s*\(\s*\w+\s*\)|defined\s+\w+|\|\||&&|==|!=|>=|<=|[!()<>]|\w+")


def _pp_eval(expr: str, bits32: bool, defines: dict[str, int] | None = None) -> bool | None:
    """Three-valued `#if` expression of a unistd header for a word size: `__BITS_PER_LONG` / `BITS_PER_LONG` is
    32 or 64, `defines` (mips' `_MIPS_SIM`) are known, `__SYSCALL_COMPAT` is undefined (the native table), every
    other macro unknown (None)."""
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
    """`text` without the conditional branches that cannot hold for the word size (`_pp_eval`); a branch whose
    condition is unknown is kept, so the result is the union of every configuration of that word size."""
    out: list[str] = []
    stack: list[list[bool | None]] = []  # per open #if: [this branch holds, an earlier branch held]
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
            stack[-1] = [v if held is False else (False if v is False else None) if held is None else v,
                         True if held is True or v is True else (None if held is None or v is None else False)]
        elif kind == "else":
            held = stack[-1][1]
            stack[-1] = [None if held is None else not held, True]
        else:
            stack.pop()
    return "\n".join(out)


def parse_syscall_table_s(text: str) -> list[str]:
    """Entries of `.long sys_xxx` tables (i386 at v3.0), in order: the index is the number."""
    return re.findall(r"^\s*\.long\s+(\w+)", text, re.M)


def _is_32bit(arch: str) -> bool:
    """ARCH=mips is the o32 table (mips64: n64)."""
    return arch in ("i386", "x86", "mips") or arch.endswith("32")


def _syscall_sources(files: FileCache, arch: str, srcarch: str, paths: set[str], bits32: bool | None = None) -> list[tuple[str, str]]:
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
    if makefile in paths:  # v6.11+ generic table: scripts/syscall.tbl (or the ARCH's syscalltbl) + its ABIs
        m = re.search(r"^syscalltbl\s*:?=\s*(\S+)", "\n".join(files.lines(makefile)), re.M)
        table = m.group(1).replace("$(srctree)/", "").replace("%", "32" if bits32 else "64") if m else "scripts/syscall.tbl"
        return [("tbl", table), ("abis", makefile)]
    cands = [f"arch/{srcarch}/kernel/syscalls/syscall.tbl", f"arch/{srcarch}/tools/syscall.tbl",
             f"arch/{srcarch}/tools/syscall_{'32' if bits32 else '64'}.tbl",
             f"arch/{srcarch}/kernel/syscalls/syscall_{'o32' if bits32 else 'n64'}.tbl"]
    for c in cands:
        if c in paths:
            return [("tbl", c)]
    for c in (f"arch/{srcarch}/include/uapi/asm/unistd.h", f"arch/{srcarch}/include/asm/unistd.h",
              "include/uapi/asm-generic/unistd.h", "include/asm-generic/unistd.h", "scripts/syscall.tbl"):
        if c in paths:
            return [("tbl" if c.endswith(".tbl") else "header", c)]
    return []


_UNISTD_INCLUDE = re.compile(r'^\s*#\s*include\s+([<"])([^>"]*unistd[^>"]*)[>"]', re.M)


def _header_text(files: FileCache, paths: set[str], path: str, srcarch: str, bits32: bool,
                 seen: set[str] | None = None) -> str:
    """A unistd header with the unistd headers it includes (`asm-generic/unistd.h`, sh's `"unistd_32.h"`, cris'
    `<arch/unistd.h>`): the numbers of score, tile, unicore32, sh and the generic-table arches live there. The
    `__BITS_PER_LONG` branches and the `_32` / `_64` includes of the other word size are left out."""
    seen = seen if seen is not None else set()
    seen.add(path)
    mips = {"_MIPS_SIM_ABI32": 1, "_MIPS_SIM_NABI32": 2, "_MIPS_SIM_ABI64": 3, "_MIPS_SIM": 1 if bits32 else 3}
    text = bits_conditional("\n".join(files.lines(path)), bits32, mips if srcarch == "mips" else None)
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


def _table_text(files: FileCache, paths: set[str], path: str) -> str:
    """A `.tbl` file's text; a symbolic link (arm64's `tools/syscall_64.tbl` -> `scripts/syscall.tbl`) is followed."""
    lines = [ln for ln in files.lines(path) if ln.strip()]
    if len(lines) == 1 and lines[0].endswith(".tbl") and not lines[0][:1].isdigit():
        target = os.path.normpath(os.path.join(os.path.dirname(path), lines[0].strip()))
        if target in paths:
            return "\n".join(files.lines(target))
    return "\n".join(lines)


def _syscall_rows(files: FileCache, paths: set[str], sources: list[tuple[str, str]], srcarch: str,
                  bits32: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    table_s = None
    for kind, path in sources:
        if kind == "tbl":
            rows = [r for r in parse_syscall_tbl(_table_text(files, paths, path)) if r["abi"] not in TBL_EXCLUDED_ABIS]
        elif kind == "abis":
            words = [f"common,{'32' if bits32 else '64'}"]
            for m in re.finditer(r"^syscall_abis_(32|64)\s*\+?=\s*(.*)$", "\n".join(files.lines(path)), re.M):
                if m.group(1) == ("32" if bits32 else "64"):
                    words.append(m.group(2))
            abis = set(re.split(r"[\s,]+", " ".join(words))) - {""}
            rows = [r for r in rows if r["abi"] in abis]
        elif kind == "table_S":
            table_s = parse_syscall_table_s("\n".join(files.lines(path)))
        else:
            rows = parse_unistd_header(_header_text(files, paths, path, srcarch, bits32), bits32)
    if table_s is not None:
        by_nr = {r["nr"]: r for r in rows}
        rows = [{"nr": i, "abi": "i386", "name": by_nr[i]["name"] if i in by_nr else e.removeprefix("sys_"),
                 "entry": e, "compat_entry": ""} for i, e in enumerate(table_s)]
    return rows


def _entry_key(r: dict[str, Any]) -> str:
    """The `SYSCALL_DEFINE` entry name a table row points at (`__x64_sys_read`, `ptregs_fork`, `read` -> `sys_*`)."""
    key = re.sub(r"^(?:__x64_|__ia32_|__se_|ptregs_|stub_|sys32_)", "", r["entry"] or f"sys_{r['name']}")
    return key if key.startswith(("sys_", "compat_sys_")) else "sys_" + key


def _is_ni(r: dict[str, Any]) -> bool:
    return r["entry"] in ("sys_ni_syscall", "ni_syscall") or r["name"] == "ni_syscall"


def _numbered_elsewhere(files: FileCache, paths: set[str], arch: str, srcarch: str) -> dict[str, set[str]]:
    """`sys_<name>` -> the other ARCHes whose own syscall table numbers it (not as `sys_ni_syscall`). x86 counts
    as `i386` and `x86_64`; every other SRCARCH is one name, the union of its 32- and 64-bit tables (arm64 and
    ia64: the 64-bit one)."""
    out: dict[str, set[str]] = defaultdict(set)
    for sa in sorted({p.split("/", 2)[1] for p in paths if p.startswith("arch/") and p.count("/") >= 2}):
        if sa == "x86":
            variants = [("i386", "i386", True), ("x86_64", "x86_64", False)]
        elif sa.endswith("64"):  # arm64 / ia64: the 32-bit table of arm64 is the AArch32 compat one
            variants = [(sa, sa, False)]
        else:  # mips: o32 + n64
            variants = [(sa, sa, True), (sa, f"{sa}64", False)]
        for label, arch_name, bits32 in variants:
            if sa == srcarch and (sa != "x86" or bits32 == _is_32bit(arch)):
                continue
            srcs = _syscall_sources(files, arch_name, sa, paths, bits32)
            if not any(p.startswith(f"arch/{sa}/") for _k, p in srcs):
                continue  # no table of its own (um, arches only the asm-generic fallback matched)
            for r in _syscall_rows(files, paths, srcs, sa, bits32):
                if not _is_ni(r):
                    out[_entry_key(r)].add(label)
                    out[f"sys_{r['name']}"].add(label)
    return out


def _syscall_defs(records: list[tuple[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """`SYSCALL_DEFINEn` / `COMPAT_SYSCALL_DEFINEn` of the `.c` files: entry name -> the first definition."""
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


def build_syscalls(sha: str, arch: str, progress: Callable | None = None) -> dict[str, Any]:
    from webapp.backend.services.kconfig_service import KconfigService
    info = KconfigService().resolve_arch(sha, arch)
    from webapp.backend.services.tree_resolver import git_tree_resolver
    paths = set((git_tree_resolver.list_tree(sha) or {}).keys())
    files = FileCache(sha)
    sources = _syscall_sources(files, info["arch"], info["srcarch"], paths)
    rows = _syscall_rows(files, paths, sources, info["srcarch"], _is_32bit(info["arch"]))
    defs = _syscall_defs(revision_records(sha, progress))
    used = set()
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
    elsewhere = _numbered_elsewhere(files, paths, info["arch"], info["srcarch"])
    unnumbered = [{"name": k.removeprefix("sys_"), **v, "numbered_on": sorted(elsewhere.get(k, ()))}
                  for k, v in sorted(defs.items()) if k.startswith("sys_") and k not in used]
    return {"arch": info["arch"], "srcarch": info["srcarch"], "sources": [{"kind": k, "path": p} for k, p in sources],
            "syscalls": rows, "unnumbered": unnumbered}


def syscalls(version: str, arch: str, wait: float = 0.0) -> dict[str, Any]:
    return _catalog("syscalls", version, lambda sha, p: build_syscalls(sha, arch, p), arch=arch, wait=wait)


# ------------------------------------------------------------------------------- A2 exports

def build_exports(sha: str, progress: Callable | None = None) -> dict[str, Any]:
    out = []
    for path, rec in revision_records(sha, progress):
        for symbol, variant, namespace, line in rec.get("x", ()):
            out.append({"symbol": symbol, "variant": variant, "namespace": namespace, "path": path, "line": line})
    counts: dict[str, int] = defaultdict(int)
    for e in out:
        counts[e["variant"]] += 1
    return {"exports": out, "total": len(out), "counts": dict(counts)}


def exports(version: str, wait: float = 0.0) -> dict[str, Any]:
    return _catalog("exports", version, build_exports, wait=wait)


def export_detail(version: str, symbol: str, arch: str) -> dict[str, Any]:
    if not re.fullmatch(r"\w{1,128}", symbol or ""):
        raise HTTPException(status_code=400, detail="Invalid symbol")
    st = exports(version, wait=60.0)
    if st.get("state") != "done":
        raise HTTPException(status_code=503, detail="The export catalog is still being built, retry")
    sites = [e for e in st["result"]["exports"] if e["symbol"] == symbol]
    if not sites:
        raise HTTPException(status_code=404, detail=f"{symbol} is not exported at {version}")
    idx = _kbuild_files(version, arch)
    for s in sites:
        s["owner"] = _owner(idx, s["path"])
    return {"version": version, "symbol": symbol, "sites": sites}


# ------------------------------------------------------------------------------- A3 parameters

def parse_kernel_parameters_doc(text: str) -> dict[str, dict[str, Any]]:
    """Documentation/(admin-guide/)kernel-parameters.txt: `name=` entries with [TAGS] and their text."""
    out: dict[str, dict[str, Any]] = {}
    current = None
    for i, raw in enumerate(text.splitlines(), 1):
        m = re.match(r"^\t([A-Za-z0-9_.\-/]+[A-Za-z0-9_\-/])(=?)(?:\s*\[([^\]]*)\])?(.*)$", raw)
        if m and not raw.startswith("\t\t"):
            name = m.group(1)
            current = out.setdefault(name, {"name": name, "tags": [t.strip() for t in (m.group(3) or "").split(",") if t.strip()],
                                            "text": m.group(4).strip(), "line": i, "takes_value": bool(m.group(2))})
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
    """The catalog row of one parameter macro (None: not a parameter, e.g. `__setup` of a computed name)."""
    row: dict[str, Any] = {"macro": name, "path": path, "line": line, "snippet": f"{name}({body})"}
    if name in ("__setup", "early_param"):
        lit = string_value(args[0]) if args else None
        if lit is None:
            return None
        row.update(kind="setup" if name == "__setup" else "early", name=lit.rstrip("="), takes_value=lit.endswith("="),
                   handler=args[1] if len(args) > 1 else "", type="", perm="")
    elif name == "core_param":
        row.update(kind="core", name=args[0] if args else "", handler="", type=args[2] if len(args) > 2 else "",
                   perm=args[3] if len(args) > 3 else "", takes_value=True)
    else:
        named = name in ("module_param_named", "module_param_array_named", "module_param_cb", "module_param_string")
        row.update(kind="module", name=args[0] if args else "", handler="",
                   type=(args[2] if named and len(args) > 2 else args[1] if len(args) > 1 else ""),
                   perm=args[-1] if args else "", takes_value=True)
    return row if row.get("name") else None


def build_params(sha: str, arch: str, version: str, progress: Callable | None = None) -> dict[str, Any]:
    files = FileCache(sha)
    rows = []
    descs: dict[tuple[str, str], str] = {}
    from parser.kbuild_conditions import is_other_arch_path
    from webapp.backend.services.kconfig_service import KconfigService
    srcarch = KconfigService().resolve_arch(sha, arch)["srcarch"]
    for path, rec in revision_records(sha, progress):
        if not rec.get("p") or not path.endswith((".c", ".h")) or path.startswith(("tools/", "scripts/")) \
                or is_other_arch_path(path, srcarch):
            continue
        for name, line, body, args in rec["p"]:
            if name == "MODULE_PARM_DESC":
                if len(args) >= 2:
                    descs[(path, args[0])] = string_value(args[1]) or args[1]
                continue
            row = _param_row(name, path, line, body, args)
            if row is not None:
                rows.append(row)
    idx = _kbuild_files(version, arch)
    for r in rows:
        if r["kind"] == "module":
            owner = _owner(idx, r["path"])
            r["module"] = owner["modname"] if owner else os.path.splitext(os.path.basename(r["path"]))[0].replace("-", "_")
            r["full_name"] = f"{r['module']}.{r['name']}"
            r["description"] = descs.get((r["path"], r["name"]), "")
        else:
            r["full_name"] = r["name"]
    doc_path = ""
    from webapp.backend.services.tree_resolver import git_tree_resolver
    tree = git_tree_resolver.list_tree(sha) or {}
    for p in ("Documentation/admin-guide/kernel-parameters.txt", "Documentation/kernel-parameters.txt"):
        if p in tree:
            doc_path = p
            break
    docs = parse_kernel_parameters_doc("\n".join(files.lines(doc_path))) if doc_path else {}
    defined = set()
    for r in rows:
        entry = docs.get(r["full_name"]) or (docs.get(r["name"]) if r["kind"] != "module" else None)
        r["doc"] = {"line": entry["line"], "tags": entry["tags"]} if entry else None
        r["undocumented"] = r["kind"] != "module" and entry is None
        defined.add(r["full_name"])
        defined.add(r["name"])
    stale = [{"name": d["name"], "line": d["line"], "tags": d["tags"]} for d in docs.values() if d["name"] not in defined]
    return {"doc_path": doc_path, "params": rows, "stale_docs": stale, "total": len(rows)}


def params(version: str, arch: str, wait: float = 0.0) -> dict[str, Any]:
    return _catalog("params", version, lambda sha, p: build_params(sha, arch, version, p), arch=arch, wait=wait)


def param_doc(version: str, line: int) -> dict[str, Any]:
    """The documentation entry starting at `line` of kernel-parameters.txt (text only)."""
    st = params(version, "x86_64", wait=0.0)
    path = (st.get("result") or {}).get("doc_path") or "Documentation/kernel-parameters.txt"
    files = FileCache(resolve(version))
    lines = files.lines(path)
    out = []
    for raw in lines[max(0, line - 1): line + 60]:
        if out and raw.startswith("\t") and not raw.startswith("\t\t") and raw.strip():
            break
        out.append(raw)
    return {"path": path, "line": line, "text": "\n".join(out).rstrip()}


# ------------------------------------------------------------------------------- A4 tracepoints

def is_trace_header(path: str) -> bool:
    """The files the catalog reads tracepoints from: everything under `include/trace/events/`, and `*trace*.h`."""
    return path.startswith("include/trace/events/") or fnmatch.fnmatchcase(os.path.basename(path), "*trace*.h")


def build_tracepoints(sha: str, progress: Callable | None = None) -> dict[str, Any]:
    events = []
    for path, rec in revision_records(sha, progress):
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


def tracepoints(version: str, wait: float = 0.0) -> dict[str, Any]:
    return _catalog("tracepoints", version, build_tracepoints, wait=wait)


def tracepoint_callsites(version: str, name: str) -> dict[str, Any]:
    if not re.fullmatch(r"\w{1,128}", name or ""):
        raise HTTPException(status_code=400, detail="Invalid tracepoint name")
    # For parsed versions, query m_symbol_ref first
    try:
        from webapp.backend.database.pool import get_reader
        with get_reader() as reader:
            from webapp.backend.services.symbol_service import SymbolService
            from webapp.backend.services.tree_resolver import best_path, definition_arch
            _, vname, blob_paths = SymbolService._scope(reader, version)
            _, scope, bits = definition_arch(vname, None)
            tp_names = [f"trace_{name}", f"trace_{name}_rcuidle", name]
            def_rows = reader.select_rows("m_symbol_def", ["ast_id", "blob_id", "line_s"], {"name": tp_names})
            defs = [r for r in def_rows if r["blob_id"] in blob_paths and r["ast_id"]]
            if defs:
                targets = list(dict.fromkeys(r["ast_id"] for r in defs))
                ref_rows = [
                    {"blob_id": r["fid"], "line_no": r["line"]}
                    for r in reader.symbol_refs_of_targets(targets, role=3, limit=500)
                ]
                if ref_rows:
                    sites = []
                    for r in ref_rows:
                        if r["blob_id"] in blob_paths:
                            fpath = best_path(blob_paths[r["blob_id"]], scope, bits)
                            if not fpath.startswith("include/trace/events/"):
                                sites.append({"path": fpath, "line": r["line_no"], "text": f"trace_{name}(...)"})
                    if sites:
                        return {"name": name, "call_sites": sites, "truncated": len(ref_rows) >= 500}
    except Exception:
        pass
    sha = resolve(version)
    res = grep_service.grep(sha, rf"\btrace_{name}(_rcuidle)?[[:space:]]*\(", mode="regex", paths=["*.c", "*.h", "*.rs"],
                            max_matches=500, max_per_file=50)
    sites = [{"path": m["path"], "line": m["line"], "text": m["text"].strip()[:200]} for m in res["matches"]
             if not m["path"].startswith("include/trace/events/") and not m["text"].lstrip().startswith(("*", "//", "/*"))]
    return {"name": name, "call_sites": sites, "truncated": res["truncated"]}


# ------------------------------------------------------------------------------- A5 modules

def build_modules(sha: str, arch: str, version: str, progress: Callable | None = None) -> dict[str, Any]:
    from webapp.backend.services.kbuild_index_service import module_capable, required_symbols
    idx = _kbuild_files(version, arch)
    if idx is None:
        raise RuntimeError("Kbuild object index unavailable")
    meta: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path, rec in revision_records(sha, progress):
        if not rec.get("m") or path not in idx["files"] or not path.endswith((".c", ".h")):
            continue
        for macro, line, args in rec["m"]:
            meta[path].append({"macro": macro, "args": args, "path": path, "line": line})
    options: dict[int, list[str]] = {}  # per interned condition: many objects share one
    modules = []
    for o in idx["objects"]:
        expr = idx["exprs"][o["expr"]]
        if o["kind"] != "obj" or not module_capable(expr):
            continue
        md: dict[str, Any] = {"license": "", "authors": [], "description": "", "aliases": [], "firmware": [], "device_tables": [],
                              "init": "", "exit": ""}
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
        modules.append({"modname": o["modname"], "target": o["target"], "expr": expr,
                        "options": list(options[o["expr"]]), "members": [m["path"] for m in o["members"]], **md})
    return {"arch": idx["arch"], "modules": modules, "total": len(modules)}


def modules(version: str, arch: str, wait: float = 0.0) -> dict[str, Any]:
    return _catalog("modules", version, lambda sha, p: build_modules(sha, arch, version, p), arch=arch, wait=wait)


def module_detail(version: str, arch: str, modname: str) -> dict[str, Any]:
    if not re.fullmatch(r"[\w-]{1,64}", modname or ""):
        raise HTTPException(status_code=400, detail="Invalid module name")
    st = modules(version, arch, wait=120.0)
    if st.get("state") != "done":
        raise HTTPException(status_code=503, detail="The module catalog is still being built, retry")
    mod = next((m for m in st["result"]["modules"] if m["modname"] == modname.replace("-", "_")), None)
    if mod is None:
        raise HTTPException(status_code=404, detail=f"No module {modname} for ARCH={arch} at {version}")
    members = set(mod["members"])
    out = dict(mod)
    p = params(version, arch, wait=0.0)
    out["params"] = [r for r in (p.get("result") or {}).get("params", []) if r["path"] in members] if p.get("state") == "done" else None
    e = exports(version, wait=0.0)
    out["exports"] = [x for x in (e.get("result") or {}).get("exports", []) if x["path"] in members] if e.get("state") == "done" else None
    try:
        from webapp.backend.services.maintainer_service import MaintainerService
        res = MaintainerService().match_maintainers(version, paths=list(members))
        out["maintainers"] = res.get("sections") or res.get("matches") or []
    except Exception:  # noqa: BLE001
        out["maintainers"] = []
    return out
