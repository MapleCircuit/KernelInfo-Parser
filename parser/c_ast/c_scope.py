"""parser/c_ast/c_scope.py - Function-local variables, parameters and struct fields of one C file, read lexically.

Pure functions, no I/O, no libclang: the scan works on any revision and on files libclang cannot resolve (isolated
mode leaves `u8`, `struct pci_dev` or `SYSCALL_DEFINE5` unresolved, so locals get neither references nor types).
The webapp runs it per file at view time (it is not persisted); `parser/c_ast/CAST_API.md` section 14 is the
contract.

`extract_scopes(text)` -> {
    "format": SCOPE_FORMAT,
    "functions": [{"name", "line_s", "line_e", "body": [line, col, line_e, col_e], "return_type", "params": [var],
                   "macro"}, ...],
    "scopes":    [{"kind": "file" | "function" | "block" | "for", "parent", "fn", "span": [line, col, line_e, col_e]}],
    "vars":      [{"name", "kind": "param" | "local" | "static_local" | "file", "type", "base", "pointer", "array",
                   "line", "col", "scope", "fn", "init": [line, col] | None, "macro", "extern"}, ...],
    "uses":      [[var, line, col, access], ...]            access: decl, r, w, rw, addr
    "members":   [[var, line, col, path, op, access], ...]  one row per member step of a chain rooted at a variable
    "assigns":   [[var, line, col, callee], ...]            the variable assigned straight from a call
    "protos":    [{"name", "line", "return_type", "params": [{"name", "type"}]}, ...]
}
Lines are 1-based; columns are 1-based character offsets into `text` (latin-1 text: libclang byte columns).

The scan: comments, strings and character literals are blanked and preprocessor lines set aside
(`parser/source_facts.py`); `#if` / `#elif` / `#else` branches restart from the state of their `#if`, so a head or a
brace written twice does not unbalance the file. Then one pass over the tokens keeps a stack of scopes (file,
function bodies, blocks, statement expressions, `for` statements), parses declarations at statement starts, and
binds every other identifier to the innermost visible declaration before it. No macro expansion: a typedef name is
guessed from its position (`IDENT (*)* IDENT` followed by `; , = [ )`), macro declarations are known by name
(`DECLARE_*` / `DEFINE_*`, `LIST_HEAD`, `DEFINE_PER_CPU`).
"""
from __future__ import annotations

import bisect
import itertools
import re
from typing import Any

from parser.source_facts import (
    MAX_HEAD,
    NOT_CALLS,
    RETURN_ANNOTATIONS,
    TOKEN,
    TYPE_QUALIFIERS,
    function_name,
    mask,
    preprocess,
    return_type,
    type_text,
)

SCOPE_FORMAT = 1

_IDSTART = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
KEYWORDS = frozenset("""
auto break case char const continue default do double else enum extern float for goto if inline int long register
restrict return short signed sizeof static struct switch typedef union unsigned void volatile while _Bool _Complex
typeof __typeof__ __typeof asm __asm__ __asm __volatile__ __attribute__ __attribute __inline__ __inline __extension__
__label__ _Static_assert static_assert __builtin_offsetof __alignof__ _Alignof alignof __auto_type __int128
__restrict __restrict__ __const __signed__ __signed
""".split())
BUILTINS = frozenset("void char short int long float double signed unsigned _Bool _Complex __int128 __signed__ __signed".split())
STORAGE = frozenset("static register auto extern typedef inline __inline __inline__ __extension__ __thread".split())
_TAGS = frozenset(("struct", "union", "enum"))
_TYPEOF = frozenset(("typeof", "__typeof__", "__typeof"))
_ASM = frozenset(("asm", "__asm__", "__asm"))
# Attribute words written around a declarator (`static int x __read_mostly;`, `__maybe_unused`): not type words
DECL_ATTRS = (RETURN_ANNOTATIONS - STORAGE) | frozenset("""
__read_mostly __initdata __devinitdata __cpuinitdata __meminitdata __exitdata __initconst __devinitconst
__cpuinitconst __refdata __cacheline_aligned __cacheline_aligned_in_smp ____cacheline_aligned
____cacheline_aligned_in_smp ____cacheline_internodealigned_in_smp __page_aligned_data __page_aligned_bss
__nosavedata __jiffy_data __packed __aligned __section __initdata_or_module __ro_after_init __lsm_ro_after_init
__percpu_data __attribute__ __attribute __cleanup __free __counted_by __nonstring __randomize_layout
""".split())
_SECTION_WORD = re.compile(r"^__\w*(?:init|exit)(?:data|const)\w*$")
_FUNCTION_MACRO = re.compile(r"^(?:(?:COMPAT_|ARCH_)?SYSCALL_DEFINE\d|BPF_CALL_\d)$")
_PER_CPU = re.compile(r"^(?:DEFINE|DECLARE)_PER_CPU\w*$")
# Declaring macros: name -> (type of the variable named by the first argument, array)
DECL_MACROS: dict[str, tuple[str | None, bool]] = {
    "LIST_HEAD": ("struct list_head", False),
    "HLIST_HEAD": ("struct hlist_head", False),
    "DECLARE_BITMAP": ("unsigned long", True),
    "DEFINE_SPINLOCK": ("spinlock_t", False),
    "DEFINE_RAW_SPINLOCK": ("raw_spinlock_t", False),
    "DEFINE_RWLOCK": ("rwlock_t", False),
    "DEFINE_MUTEX": ("struct mutex", False),
    "DEFINE_SEMAPHORE": ("struct semaphore", False),
    "DECLARE_RWSEM": ("struct rw_semaphore", False),
    "DECLARE_COMPLETION": ("struct completion", False),
    "DECLARE_COMPLETION_ONSTACK": ("struct completion", False),
    "DECLARE_WAIT_QUEUE_HEAD": ("wait_queue_head_t", False),
    "DECLARE_WAIT_QUEUE_HEAD_ONSTACK": ("wait_queue_head_t", False),
    "DECLARE_WORK": ("struct work_struct", False),
    "DECLARE_DELAYED_WORK": ("struct delayed_work", False),
    "DECLARE_WAITQUEUE": (None, False),
    "DEFINE_WAIT": (None, False),
}
# DECLARE_* / DEFINE_* macros whose first argument is not a variable they declare
_NOT_DECL_MACROS = ("DEFINE_EVENT", "DECLARE_EVENT", "DEFINE_TRACE", "DECLARE_TRACE", "DEFINE_IDTENTRY",
                    "DECLARE_IDTENTRY", "DEFINE_SHOW", "DEFINE_SEQ_ATTRIBUTE", "DEFINE_GUARD", "DEFINE_LOCK_GUARD",
                    "DEFINE_FREE", "DEFINE_CLASS")
# Calls that write (or read and write) some of their arguments: callee -> {argument index: access}. Besides these,
# any callee containing `for_each` writes its first argument (and the second for a `_safe` iterator).
OUT_PARAMS: dict[str, dict[int, str]] = {
    "get_user": {0: "w"},
    "__get_user": {0: "w"},
    "rdmsr": {1: "w", 2: "w"},
    "rdmsr_safe": {1: "w", 2: "w"},
    "rdmsrl": {1: "w"},
    "rdmsrl_safe": {1: "w"},
    "local_irq_save": {0: "w"},
    "raw_local_irq_save": {0: "w"},
    "local_save_flags": {0: "w"},
    "raw_local_save_flags": {0: "w"},
    "spin_lock_irqsave": {1: "w"},
    "spin_lock_irqsave_nested": {1: "w"},
    "spin_trylock_irqsave": {1: "w"},
    "raw_spin_lock_irqsave": {1: "w"},
    "raw_spin_lock_irqsave_nested": {1: "w"},
    "raw_spin_trylock_irqsave": {1: "w"},
    "read_lock_irqsave": {1: "w"},
    "write_lock_irqsave": {1: "w"},
    "write_seqlock_irqsave": {1: "w"},
    "do_div": {0: "rw"},
    "swap": {0: "rw", 1: "rw"},
}
_COMPOUND = frozenset(("+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="))
_OPEN = {"(": ")", "[": "]", "{": "}"}
_CLOSE = {")": "(", "]": "[", "}": "{"}
_CTRL = frozenset(("if", "while", "for", "switch"))
_STATEMENTS = frozenset("if while for switch do else return goto break continue case default sizeof asm __asm__ __asm".split())

CTX_FILE, CTX_BLOCK, CTX_PARAM, CTX_MEMBER = range(4)
F_FILE, F_FN, F_BLOCK, F_FOR, F_INIT = range(5)


def _is_id(t: str) -> bool:
    return bool(t) and t[0] in _IDSTART


def _tokens(code: str) -> tuple[list[str], list[int]]:
    """Tokens of masked, directive-free code and their offsets, followed by four empty sentinels."""
    ms = list(TOKEN.finditer(code))
    toks = [m.group() for m in ms]
    offs = [m.start() for m in ms]
    toks.extend(("",) * 4)
    offs.extend((len(code),) * 4)
    return toks, offs


def _match(toks: list[str], offs: list[int], events: list[tuple[int, str]]) -> list[int]:
    """Index of the matching bracket of every bracket token (-1 when unmatched); `#else` branches restart from the
    open brackets of their `#if`."""
    out = [-1] * len(toks)
    stack: list[int] = []
    saved: list[list[int]] = []
    ev, ne = 0, len(events)
    for i, t in enumerate(toks):
        if ev < ne:
            o = offs[i]
            while ev < ne and events[ev][0] < o:
                kind = events[ev][1]
                if kind == "if":
                    saved.append(list(stack))
                elif kind == "else":
                    if saved:
                        stack = list(saved[-1])
                elif saved:
                    saved.pop()
                ev += 1
        if t in _OPEN:
            stack.append(i)
        elif t in _CLOSE:
            want = _CLOSE[t]
            k = len(stack) - 1
            while k >= 0 and toks[stack[k]] != want:
                k -= 1
            if k >= 0:
                j = stack[k]
                out[j] = i
                out[i] = j
                del stack[k:]
    return out


def _split_args(toks: list[str], match: list[int], a: int, b: int) -> list[tuple[int, int]]:
    """(start, end) token ranges of the comma-separated items between brackets at `a` and `b`."""
    parts: list[tuple[int, int]] = []
    st = k = a + 1
    while k < b:
        t = toks[k]
        if t in _OPEN and match[k] > k:
            k = match[k] + 1
            continue
        if t == ",":
            parts.append((st, k))
            st = k + 1
        k += 1
    parts.append((st, b))
    return parts


# ------------------------------------------------------------------------------------- types

def type_base(type_text_: str) -> dict[str, Any]:
    """`{"kind", "name", "pointer", "array"}` of a type text: the navigable base (`struct` / `union` / `enum` tag,
    typedef name, or builtin words such as `"unsigned long"`), the pointer depth and whether it is an array. `kind`
    and `name` are None without a navigable base (function pointers, `typeof(...)`, `__auto_type`)."""
    toks = TOKEN.findall(type_text_ or "")
    out: dict[str, Any] = {"kind": None, "name": None, "pointer": 0, "array": False}
    flat: list[str] = []
    k, n = 0, len(toks)
    while k < n:  # array dimensions and typeof arguments are not part of the base
        t = toks[k]
        if t == "[" or (t in _TYPEOF and k + 1 < n and toks[k + 1] == "("):
            out["array"] = out["array"] or t == "["
            close, depth = (k, 0) if t == "[" else (k + 1, 0)
            j = close
            while j < n:
                if toks[j] in ("(", "["):
                    depth += 1
                elif toks[j] in (")", "]"):
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            flat.append("[]" if t == "[" else "typeof")
            k = j + 1
            continue
        flat.append(t)
        k += 1
    for j, t in enumerate(flat):
        if t == "(" and j + 1 < len(flat) and flat[j + 1] == "*":
            depth, stars = 0, 0
            for u in flat[j:]:
                if u == "(":
                    depth += 1
                elif u == ")":
                    depth -= 1
                    if depth == 0:
                        break
                elif u == "*" and depth == 1:
                    stars += 1
            out["pointer"] = stars
            return out
    out["pointer"] = flat.count("*")
    words = [t for t in flat if _is_id(t) and t not in TYPE_QUALIFIERS and t not in STORAGE and t not in DECL_ATTRS]
    if not words or words[0] in ("typeof", "__auto_type"):
        return out
    if words[0] in _TAGS:
        if len(words) > 1 and words[1] not in KEYWORDS:
            out["kind"], out["name"] = words[0], words[1]
        return out
    builtin = [w for w in words if w in BUILTINS]
    if words[0] in BUILTINS:
        out["kind"], out["name"] = "builtin", " ".join(builtin)
    else:
        out["kind"], out["name"] = "typedef", words[0]
    return out


def _info(type_toks: list[str], **extra: Any) -> dict[str, Any]:
    text = type_text(type_toks) or None
    tb = type_base(text or "")
    base = {"kind": tb["kind"], "name": tb["name"]} if tb["kind"] else None
    info = {"type": text, "base": base, "pointer": tb["pointer"], "array": tb["array"], "name": None, "init": None,
            "macro": None, "func": False}
    info.update(extra)
    return info


# ------------------------------------------------------------------------------------- declarations

def _skip_attr(toks: list[str], match: list[int], i: int) -> int:
    """Past an attribute word at `i` and its argument group, if any."""
    if toks[i + 1] == "(" and match[i + 1] > i:
        return match[i + 1] + 1
    return i + 1


def _is_attr(t: str) -> bool:
    return t in DECL_ATTRS or bool(_SECTION_WORD.match(t))


def _macro_decl(toks: list[str], match: list[int], i: int, end: int, storage: list[str]) -> dict[str, Any] | None:
    """`DEFINE_SPINLOCK(lock);`, `DEFINE_PER_CPU(type, name)`, `LIST_HEAD(list);` ... at `i`."""
    t = toks[i]
    close = match[i + 1]
    if close < 0 or close >= end:
        return None
    args = _split_args(toks, match, i + 1, close)
    keep: list[tuple[int, int]] = []
    if _PER_CPU.match(t):
        if len(args) < 2:
            return None
        a, b = args[-1]
        name = next((k for k in range(a, b) if _is_id(toks[k]) and toks[k] not in KEYWORDS), None)
        if name is None:
            return None
        ta, tb = args[0]
        info = _info([x for x in toks[ta:tb] if not _is_attr(x)], macro=t)
        info["array"] = info["array"] or "[" in toks[a:b]
    else:
        a, b = args[0]
        if b != a + 1 or not _is_id(toks[a]) or toks[a] in KEYWORDS:
            return None
        name = a
        if t in DECL_MACROS:
            typ, arr = DECL_MACROS[t]
        elif t.startswith(("DECLARE_", "DEFINE_")) and not t.startswith(_NOT_DECL_MACROS):
            typ, arr = None, False
        else:
            return None
        info = _info(TOKEN.findall(typ) if typ else [], macro=t)
        info["array"] = arr
        if close > a + 1:
            keep.append((a + 1, close))
    info["name"] = name
    k = close + 1
    while _is_id(toks[k]) and toks[k] not in KEYWORDS:
        k = _skip_attr(toks, match, k)
    if toks[k] == "=":
        info["init"] = k + 1
        j = k + 1
        while j < end and toks[j] not in (",", ";"):
            if toks[j] in _OPEN:
                if match[j] < 0:
                    return None
                j = match[j] + 1
            elif toks[j] in _CLOSE:
                break
            else:
                j += 1
        keep.append((k + 1, j))
        k = j
    if toks[k] != ";":
        return None
    return {"storage": storage, "kind": "macro", "tag": None, "body": None, "items": [info], "stop": k, "keep": keep}


def _decl(toks: list[str], match: list[int], s: int, end: int, ctx: int) -> dict[str, Any] | None:
    """The declaration starting at token `s` (not reaching `end`), or None when the tokens are no declaration.
    {"storage", "kind", "tag", "body": (open, close) | None, "items": [info], "stop": terminator index,
    "keep": [(a, b)] token ranges holding expressions (array sizes, initializers, macro arguments)}."""
    i = s
    storage: list[str] = []
    spec: list[str] = []
    kind: str | None = None
    tag: str | None = None
    td_at = -1
    body: tuple[int, int] | None = None
    quals = False
    while i < end:
        t = toks[i]
        if t in STORAGE:
            storage.append(t)
            i += 1
        elif t in TYPE_QUALIFIERS:
            spec.append(t)
            quals = True
            i += 1
        elif t in BUILTINS or t in _TAGS:
            if kind == "typedef" and tag is not None and tag.startswith("__"):
                del spec[td_at]  # an attribute word taken for a type name (`static __initdata int x;`)
                kind = tag = None
            if t in BUILTINS:
                if kind not in (None, "builtin"):
                    break
                kind = "builtin"
                spec.append(t)
                i += 1
                continue
            if kind is not None:
                break
            kind = t
            spec.append(t)
            i += 1
            while _is_attr(toks[i]) or (toks[i].startswith("__") and toks[i + 1] == "(" and toks[i] not in _TYPEOF):
                i = _skip_attr(toks, match, i)
            if _is_id(toks[i]) and toks[i] not in KEYWORDS:
                tag = toks[i]
                spec.append(tag)
                i += 1
            while _is_attr(toks[i]):
                i = _skip_attr(toks, match, i)
            if toks[i] == "{":
                close = match[i]
                if close < 0 or close >= end:
                    return None
                body = (i, close)
                if tag is None:
                    spec.append("{...}")
                i = close + 1
        elif t in _TYPEOF and toks[i + 1] == "(":
            close = match[i + 1]
            if kind is not None or close < 0 or close >= end:
                break
            kind = "typeof"
            spec.extend(toks[i:close + 1])
            i = close + 1
        elif t == "__auto_type":
            if kind is not None:
                break
            kind = "auto"
            spec.append(t)
            i += 1
        elif _is_id(t):
            if kind is None and toks[i + 1] == "(" and (t in DECL_MACROS or _PER_CPU.match(t)
                                                        or t.startswith(("DECLARE_", "DEFINE_"))):
                if ctx == CTX_PARAM:
                    return None
                return _macro_decl(toks, match, i, end, storage)
            if _is_attr(t) or (t.startswith("__") and toks[i + 1] == "("):
                i = _skip_attr(toks, match, i)
                continue
            if t in KEYWORDS:
                if kind is None:
                    return None
                break
            if kind is not None:
                break
            kind, tag, td_at = "typedef", t, len(spec)
            spec.append(t)
            i += 1
        else:
            break
    if kind is None:
        return None
    typedef_only = kind == "typedef" and not storage and not quals
    if typedef_only and ctx in (CTX_BLOCK, CTX_FILE):
        j = i
        while toks[j] == "*":
            j += 1
        if toks[j] == "(" and toks[j + 1] == "*":
            if match[j] < 0 or toks[match[j] + 1] != "(":
                return None
        elif not (_is_id(toks[j]) and toks[j] not in KEYWORDS
                  and (toks[j + 1] in (";", ",", "=", "[", ")") or _is_attr(toks[j + 1])
                       or (ctx == CTX_FILE and toks[j + 1] == "("))):
            return None
    items: list[dict[str, Any]] = []
    keep: list[tuple[int, int]] = []
    while True:
        dt: list[str] = []
        while True:
            t = toks[i]
            if t == "*" or t in TYPE_QUALIFIERS:
                dt.append(t)
                i += 1
            elif _is_attr(t):
                i = _skip_attr(toks, match, i)
            else:
                break
        name = None
        fnptr = False
        inner: list[str] = []
        dims: list[str] = []
        t = toks[i]
        if t == "(" and toks[i + 1] in ("*", "^"):
            close = match[i]
            if close < 0 or close >= end:
                return None
            j = i + 1
            while toks[j] == "*" or toks[j] in TYPE_QUALIFIERS:
                inner.append(toks[j])
                j += 1
            if _is_id(toks[j]) and toks[j] not in KEYWORDS:
                name = j
                j += 1
            while toks[j] == "[" and 0 < match[j] < close:
                keep.append((j + 1, match[j]))
                j = match[j] + 1
            if j != close:
                return None
            i = close + 1
            if toks[i] == "(" and match[i] > i:
                fnptr = True
                fparams = toks[i:match[i] + 1]
                i = match[i] + 1
        elif _is_id(t) and t not in KEYWORDS:
            name = i
            i += 1
        elif not (ctx in (CTX_PARAM, CTX_MEMBER) or t in (";", ",")):
            return None
        while toks[i] == "[" and i < match[i] < end:
            dims.extend(toks[i:match[i] + 1])
            keep.append((i + 1, match[i]))
            i = match[i] + 1
        func = False
        if toks[i] == "(" and not fnptr and name is not None and not inner:
            if match[i] < 0 or match[i] >= end:
                return None
            func = True
            i = match[i] + 1
        while _is_id(toks[i]) and (toks[i] not in KEYWORDS or toks[i] in _ASM or toks[i] in ("__attribute__", "__attribute")):
            i = _skip_attr(toks, match, i)
        if toks[i] == ":" and ctx == CTX_MEMBER:  # a bit-field width
            i += 1
            while i < end and toks[i] not in (",", ";"):
                i = match[i] + 1 if toks[i] in _OPEN and match[i] > i else i + 1
        init = None
        if toks[i] == "=" and ctx != CTX_PARAM:
            init = i + 1
            j = i + 1
            while j < end and toks[j] not in (",", ";"):
                if toks[j] in _OPEN:
                    if match[j] < 0:
                        return None
                    j = match[j] + 1
                elif toks[j] in _CLOSE:
                    break
                else:
                    j += 1
            keep.append((init, j))
            i = j
        if fnptr:
            info = _info(spec + dt + ["("] + inner + [")"] + fparams)
            info["pointer"] = inner.count("*")
        else:
            info = _info(spec + dt + dims)
        info["name"], info["init"], info["func"] = name, init, func
        if name is not None or ctx == CTX_PARAM:
            items.append(info)
        if toks[i] == "," and ctx != CTX_PARAM:
            i += 1
            continue
        break
    if ctx == CTX_PARAM:
        if i != end:
            return None
    elif toks[i] != ";" or i > end:
        return None
    if typedef_only and not any(it["name"] is not None for it in items):
        return None
    return {"storage": storage, "kind": kind, "tag": tag, "body": body, "items": items, "stop": i, "keep": keep}


def _params(toks: list[str], match: list[int], a: int, b: int, macro: str | None) -> list[dict[str, Any]]:
    """Parameter infos of the parameter list between `a` and `b` (`name` the name token or None); for
    `SYSCALL_DEFINEn(name, type, arg, ...)` / `BPF_CALL_n` the type and name pairs."""
    parts = _split_args(toks, match, a, b)
    out: list[dict[str, Any]] = []
    if macro:
        rest = parts[1:]
        for j in range(0, len(rest) - 1, 2):
            (ta, tb), (na, nb) = rest[j], rest[j + 1]
            ni = next((k for k in range(nb - 1, na - 1, -1) if _is_id(toks[k]) and toks[k] not in KEYWORDS), None)
            info = _info([x for x in toks[ta:tb] if not _is_attr(x)])
            info["name"] = ni
            out.append(info)
        return out
    if len(parts) == 1 and parts[0][1] == parts[0][0] + 1 and toks[parts[0][0]] == "void":
        return out
    for x, y in parts:
        if y <= x:
            continue
        if toks[x] == "...":
            info = _info([], ellipsis=True)
            info["type"] = "..."
            out.append(info)
            continue
        d = _decl(toks, match, x, y, CTX_PARAM)
        if d is None or not d["items"]:
            out.append(_info([t for t in toks[x:y] if not _is_attr(t)]))
        else:
            out.append(d["items"][0])
    return out


def _proto_params(toks: list[str], infos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"name": "..." if info.get("ellipsis") else (toks[info["name"]] if info["name"] is not None else None),
             "type": info["type"]} for info in infos]


def parse_signature(text: str) -> dict[str, Any] | None:
    """`{"name", "return_type", "params": [{"name", "type"}], "macro"}` of a function head or prototype text
    (`struct pci_dev *pci_get_bus_and_slot(unsigned int bus, unsigned int devfn)`, multi-line heads, a trailing
    `{` or `;`), or None when the text is no function head (a macro, a struct)."""
    if not text:
        return None
    try:
        masked = mask(text)
        if masked.lstrip().startswith("#"):
            return None
        _events, code = preprocess(masked)
        for stop in ("{", ";"):
            cut = code.find(stop)
            if cut >= 0:
                code = code[:cut]
        head = code.rstrip()
        while True:
            m = re.search(r"([A-Za-z_]\w*)\s*$", head)
            if m and (m.group(1) in RETURN_ANNOTATIONS or _SECTION_WORD.match(m.group(1))):
                head = head[:m.start()].rstrip()
                continue
            break
        found = function_name(head)
        if found is None:
            return None
        name, off = found
        toks, offs = _tokens(head)
        match = _match(toks, offs, [])
        ni = bisect.bisect_left(offs, off)
        macro = toks[ni] if _FUNCTION_MACRO.match(toks[ni]) else None
        params: list[dict[str, Any]] = []
        if toks[ni + 1] == "(" and match[ni + 1] > ni:
            params = _proto_params(toks, _params(toks, match, ni + 1, match[ni + 1], macro))
        return {"name": name, "return_type": return_type(head, off) or None, "params": params, "macro": macro}
    except (IndexError, ValueError, RecursionError):
        return None


# ------------------------------------------------------------------------------------- struct members

def find_member(text: str, line_s: int, line_e: int, field: str) -> dict[str, Any] | None:
    """The declaration of `field` inside the struct/union body spanning lines `line_s..line_e` of `text`:
    `{"line", "col", "type", "base", "pointer", "array"}`, or None. Fields of nested anonymous structs and unions
    belong to the outer struct; bit-fields, arrays, function-pointer members and `int a, *b;` are handled."""
    try:
        starts = [0, *itertools.accumulate(len(x) + 1 for x in text.split("\n"))]
        if line_s < 1 or line_s >= len(starts):
            return None
        a = starts[line_s - 1]
        b = starts[min(line_e, len(starts) - 1)]
        region = text[a:b]
        events, code = preprocess(mask(region))
        toks, offs = _tokens(code)
        match = _match(toks, offs, events)
        n = len(toks) - 4
        k0 = next((k for k in range(n) if toks[k] == "{"), None)
        if k0 is None:
            return None
        close = match[k0] if match[k0] > k0 else n
        info = _member(toks, match, k0, close, field)
        if info is None:
            return None
        off = a + offs[info["name"]]
        line = bisect.bisect_right(starts, off)
        return {"line": line, "col": off - starts[line - 1] + 1, "type": info["type"], "base": info["base"],
                "pointer": info["pointer"], "array": info["array"]}
    except (IndexError, ValueError, RecursionError):
        return None


def _member(toks: list[str], match: list[int], open_: int, close: int, field: str, depth: int = 0) -> dict[str, Any] | None:
    i = open_ + 1
    while i < close:
        if toks[i] == ";":
            i += 1
            continue
        d = _decl(toks, match, i, close, CTX_MEMBER)
        if d is None:
            while i < close and toks[i] != ";":
                i = match[i] + 1 if toks[i] in _OPEN and match[i] > i else i + 1
            i += 1
            continue
        named = [it for it in d["items"] if it["name"] is not None]
        if d["body"] and not named and d["tag"] is None and d["kind"] in ("struct", "union") and depth < 8:
            found = _member(toks, match, d["body"][0], d["body"][1], field, depth + 1)
            if found is not None:
                return found
        for it in named:
            if toks[it["name"]] == field:
                return it
        i = d["stop"] + 1
    return None


# ------------------------------------------------------------------------------------- the scan

def extract_scopes(text: str, masked: str | None = None) -> dict[str, Any]:
    """The scopes, variables and their uses of one C file (see the module docstring); `masked` is
    `source_facts.mask(text)` when the caller has it. Never raises: odd input gives a partial or empty model."""
    model: dict[str, Any] = {"format": SCOPE_FORMAT, "functions": [], "scopes": [], "vars": [], "uses": [],
                             "members": [], "assigns": [], "protos": []}
    try:
        _Scan(text or "", masked, model).run()
    except (IndexError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        _Scan.finish(model, text or "")
    return model


class _Scan:
    def __init__(self, text: str, masked: str | None, model: dict[str, Any]) -> None:
        self.text = text
        self.model = model
        if masked is None or len(masked) != len(text):
            masked = mask(text)
        self.events, self.code = preprocess(masked)
        self.toks, self.offs = _tokens(self.code)
        self.match = _match(self.toks, self.offs, self.events)
        self.starts = [0, *itertools.accumulate(len(x) + 1 for x in text.split("\n"))]
        self.names: list[dict[str, list[int]]] = []
        self.frames: list[list[Any]] = []
        self.parens: list[list[Any]] = []
        self.jump: list[int] = [0] * len(self.toks)  # token -> the next token to walk (0: walk this one)
        self.decl_at: dict[int, dict[str, Any]] = {}
        self.pending_fn: dict[int, tuple[int, int]] = {}

    # -- positions and records
    def pos(self, off: int) -> tuple[int, int]:
        li = bisect.bisect_right(self.starts, off)
        return li, off - self.starts[li - 1] + 1

    def new_scope(self, kind: str, parent: int | None, fn: int | None, off: int) -> int:
        scopes = self.model["scopes"]
        line, col = self.pos(off)
        scopes.append({"kind": kind, "parent": parent, "fn": fn, "span": [line, col, None, None]})
        self.names.append({})
        return len(scopes) - 1

    def end_frame(self, frame: list[Any], off: int) -> None:
        line, col = self.pos(off)
        if frame[1] >= 0:
            span = self.model["scopes"][frame[1]]["span"]
            span[2], span[3] = line, col
        if frame[0] == F_FN and frame[4] is not None:
            fn = self.model["functions"][frame[4]]
            fn["line_e"] = line
            fn["body"][2], fn["body"][3] = line, col

    def cur_scope(self) -> int:
        for f in reversed(self.frames):
            if f[1] >= 0:
                return f[1]
        return 0

    def resolve(self, name: str) -> int | None:
        names = self.names
        for f in reversed(self.frames):
            sc = f[1]
            if sc >= 0:
                lst = names[sc].get(name)
                if lst:
                    return lst[-1]
        return None

    def add_var(self, ni: int, info: dict[str, Any], scope: int, fn: int | None, kind: str, macro: str | None = None) -> int:
        vars_ = self.model["vars"]
        idx = len(vars_)
        name = self.toks[ni]
        line, col = self.pos(self.offs[ni])
        init = info.get("init")
        vars_.append({"name": name, "kind": kind, "type": info["type"], "base": info["base"], "pointer": info["pointer"],
                      "array": info["array"], "line": line, "col": col, "scope": scope, "fn": fn,
                      "init": list(self.pos(self.offs[init])) if init is not None else None,
                      "macro": info.get("macro") or macro, "extern": bool(info.get("extern"))})
        self.names[scope].setdefault(name, []).append(idx)
        self.model["uses"].append([idx, line, col, "decl"])
        if init is not None:
            callee = self.call_at(init)
            if callee:
                self.model["assigns"].append([idx, line, col, callee])
        return idx

    # -- expression shapes
    def is_cast(self, a: int, b: int) -> bool:
        """The parenthesized group `a..b` is a cast or a compound literal's type."""
        toks = self.toks
        if a < 0 or b - a < 2:
            return False
        pre = toks[a - 1] if a > 0 else ""
        if pre in _CTRL or pre in ("sizeof", ")", "]") or pre in _TYPEOF or (_is_id(pre) and pre not in KEYWORDS):
            return False
        for k in range(a + 1, b):
            t = toks[k]
            if t == "*":
                continue
            if not _is_id(t) or (t in KEYWORDS and t not in _TAGS and t not in BUILTINS and t not in TYPE_QUALIFIERS):
                return False
        return not (b - a == 2 and self.resolve(toks[a + 1]) is not None)

    def unary(self, p: int) -> bool:
        """The `&` / `*` at `p` is a unary operator."""
        if p <= 0:
            return True
        q = self.toks[p - 1]
        if q == ")":
            return self.is_cast(self.match[p - 1], p - 1)
        if q == "]" or q[:1].isdigit():
            return False
        if _is_id(q):
            return q in KEYWORDS
        return True

    def call_at(self, k: int) -> str | None:
        """The callee when the expression at `k` is a direct call (through an optional cast) ending the expression."""
        toks, match = self.toks, self.match
        if toks[k] == "(" and match[k] > k and self.is_cast(k, match[k]):
            k = match[k] + 1
        t = toks[k]
        if (_is_id(t) and t not in KEYWORDS and t not in NOT_CALLS and toks[k + 1] == "(" and match[k + 1] > k
                and toks[match[k + 1] + 1] in (";", ",", ")")):
            return t
        return None

    @staticmethod
    def out_access(callee: str, arg: int) -> str | None:
        m = OUT_PARAMS.get(callee)
        if m is not None:
            return m.get(arg)
        if "for_each" in callee and (arg == 0 or (arg == 1 and callee.endswith("_safe"))):
            return "w"
        return None

    def use(self, v: int, i: int) -> None:
        toks, match, offs = self.toks, self.match, self.offs
        path: list[str] = []
        steps: list[tuple[int, int, str]] = []
        j = i + 1
        while True:
            t = toks[j]
            if (t == "->" or t == ".") and _is_id(toks[j + 1]):
                path.append(toks[j + 1])
                steps.append((j + 1, len(path), t))
                j += 2
            elif t == "[" and match[j] > j:
                path.append("[]")
                j = match[j] + 1
            else:
                break
        after = toks[j]
        p = i - 1
        before = toks[p] if p >= 0 else ""
        acc = "r"
        if after == "=":
            acc = "w"
        elif after in _COMPOUND or after in ("++", "--") or before in ("++", "--"):
            acc = "rw"
        elif before == "&" and self.unary(p):
            acc = "addr"
        elif (before == "(" or before == ",") and (after == "," or after == ")") and len(self.parens) > self.frames[-1][2]:
            pe = self.parens[-1]
            if pe[0]:
                acc = self.out_access(pe[0], pe[1]) or "r"
        if acc in ("w", "rw") and before == "*" and after not in ("++", "--") and self.unary(p):
            acc = "r"  # `*p = x` writes the pointee
        line, col = self.pos(offs[i])
        if steps:
            last = len(steps) - 1
            members = self.model["members"]
            for k, (ti, plen, op) in enumerate(steps):
                ml, mc = self.pos(offs[ti])
                members.append([v, ml, mc, path[:plen], op, acc if k == last else "r"])
            base = "w" if acc in ("w", "rw") and all(op == "." for _ti, _pl, op in steps) else "r"
            self.model["uses"].append([v, line, col, base])
            return
        if path and acc in ("w", "rw") and not self.model["vars"][v]["array"]:
            acc = "r"  # `p[i] = x` through a pointer
        self.model["uses"].append([v, line, col, acc])
        if not path and after == "=":
            callee = self.call_at(j + 1)
            if callee:
                self.model["assigns"].append([v, line, col, callee])

    # -- declarations
    def annotate(self, d: dict[str, Any], s: int, kind_of: str) -> None:
        """Walk only the expressions of a parsed declaration; its names become variables when reached."""
        marks = [(a, b) for a, b in d["keep"] if a < b]
        if "typedef" not in d["storage"]:
            for it in d["items"]:
                if it["name"] is not None and not it["func"]:
                    it["kind"] = kind_of
                    it["extern"] = "extern" in d["storage"]
                    self.decl_at[it["name"]] = it
                    marks.append((it["name"], it["name"] + 1))
        marks.sort()
        cur = s
        for a, b in marks:
            if a > cur:
                self.jump[cur] = a
            cur = max(cur, b)
        if cur < d["stop"]:
            self.jump[cur] = d["stop"]

    def file_statement(self, s: int) -> int:
        """At a file-level statement start: a function definition (returns its body's brace), a declaration
        (annotated; returns `s`), or something else (returns where to go on)."""
        toks, offs, match, code = self.toks, self.offs, self.match, self.code
        n = len(toks) - 4
        k = s
        while k < n:
            t = toks[k]
            if t == ";" or t == "}":
                break
            if t == "(" or t == "[":
                if match[k] < 0:
                    return n
                k = match[k] + 1
                continue
            if t == "{":
                prev = toks[k - 1] if k > s else ""
                if prev != "=" and prev not in _TAGS and not (k - 2 >= s and toks[k - 2] in _TAGS):
                    head_off = max(offs[s], offs[k] - MAX_HEAD)
                    head = code[head_off:offs[k]]
                    found = function_name(head)
                    if found is not None:
                        return self.function(s, k, head, head_off, found)
                if match[k] < 0:
                    return n
                k = match[k] + 1
                continue
            k += 1
        d = _decl(toks, match, s, k, CTX_FILE) if k < n and toks[k] == ";" else None
        if d is None:
            if _is_id(toks[s]) and toks[s + 1] == "(" and s + 1 < match[s + 1] < k - 1:
                self.restart = True
                return match[s + 1] + 1  # a macro call without `;` before the next statement
            return max(k, s + 1)
        if "typedef" not in d["storage"]:
            for it in d["items"]:
                fi = it["name"]
                if it["func"] and toks[fi + 1] == "(":
                    self.model["protos"].append({
                        "name": toks[fi], "line": self.pos(offs[fi])[0],
                        "return_type": return_type(code[offs[s]:offs[fi + 1]], offs[fi] - offs[s]) or None,
                        "params": _proto_params(toks, _params(toks, match, fi + 1, match[fi + 1], None))})
        self.annotate(d, s, "file")
        return s

    def function(self, s: int, k: int, head: str, head_off: int, found: tuple[str, int]) -> int:
        toks, offs, match = self.toks, self.offs, self.match
        name, off = found
        ni = bisect.bisect_left(offs, head_off + off)
        macro = toks[ni] if _FUNCTION_MACRO.match(toks[ni]) else None
        rtype = return_type(head, off) or None
        functions = self.model["functions"]
        fn = len(functions)
        close = match[k] if match[k] > k else len(toks) - 4
        l1, c1 = self.pos(offs[k])
        l2, c2 = self.pos(offs[close] if close < len(toks) - 4 else max(0, len(self.text) - 1))
        line_s = self.pos(offs[ni])[0]
        functions.append({"name": name, "line_s": line_s, "line_e": l2, "body": [l1, c1, l2, c2],
                          "return_type": rtype, "params": [], "macro": macro})
        scope = self.new_scope("function", 0, fn, offs[k])
        infos: list[dict[str, Any]] = []
        if toks[ni + 1] == "(" and match[ni + 1] > ni:
            infos = _params(toks, match, ni + 1, match[ni + 1], macro)
        for info in infos:
            if info["name"] is not None:
                functions[fn]["params"].append(self.add_var(info["name"], info, scope, fn, "param", macro))
        self.model["protos"].append({"name": name, "line": line_s, "return_type": rtype,
                                     "params": _proto_params(toks, infos)})
        self.pending_fn[k] = (fn, scope)
        return k

    # -- the pass
    def run(self) -> None:
        toks, offs, match = self.toks, self.offs, self.match
        events = self.events
        n = len(toks) - 4
        self.new_scope("file", None, None, 0)
        frames, parens = self.frames, self.parens
        frames.append([F_FILE, 0, 0, False, None])
        saved: list[tuple[Any, ...]] = []
        stmt, colon = True, False
        jump, decl_at, pending_fn = self.jump, self.decl_at, self.pending_fn
        resolve, use = self.resolve, self.use
        ev, ne = 0, len(events)
        nxt = events[0][0] if events else len(self.code) + 1
        parsed = -1
        self.restart = False
        i = 0
        while i < n:
            o = offs[i]
            while o > nxt:
                kind = events[ev][1]
                if kind == "if":
                    saved.append((list(frames), [list(p) for p in parens], stmt, colon))
                elif kind == "else":
                    if saved:
                        fr, pa, stmt, colon = saved[-1]
                        kept = {id(f) for f in fr}
                        for f in frames:
                            if id(f) not in kept:
                                self.end_frame(f, nxt)
                        frames[:] = fr
                        parens[:] = [list(p) for p in pa]
                elif saved:
                    saved.pop()
                ev += 1
                nxt = events[ev][0] if ev < ne else len(self.code) + 1
            j = jump[i]
            if j:
                i = j
                continue
            t = toks[i]
            top = frames[-1]
            at_stmt, stmt = stmt, False
            if t[0] in _IDSTART:
                if at_stmt and parsed != i:
                    parsed = i
                    if top[0] == F_FILE:
                        ni = self.file_statement(i)
                        if ni != i:
                            if self.restart:
                                self.restart = False
                                stmt = True
                            i = ni
                            continue
                        if jump[i] or i in decl_at:
                            continue
                    elif t not in _STATEMENTS:
                        if toks[i + 1] == ":" and t not in KEYWORDS:
                            colon = True  # a label
                            i += 1
                            continue
                        d = _decl(toks, match, i, n, CTX_BLOCK)
                        if d is not None:
                            static = top[4] is not None and "static" in d["storage"]
                            self.annotate(d, i, "static_local" if static else "local")
                            if jump[i] or i in decl_at:
                                continue
                if t in KEYWORDS:
                    if t == "for" and toks[i + 1] == "(":
                        sc = self.new_scope("for", self.cur_scope(), top[4], o)
                        frames.append([F_FOR, sc, len(parens), False, top[4]])
                    elif t == "else" or t == "do":
                        stmt = True
                    elif t == "case" or (t == "default" and toks[i + 1] == ":"):
                        colon = True
                    elif t == "goto" or (t in _TAGS and toks[i + 1][:1] in _IDSTART):
                        i += 2
                        continue
                elif i in decl_at:
                    sc = self.cur_scope()
                    info = decl_at[i]
                    self.add_var(i, info, sc, top[4], info.get("kind", "local") if sc else "file")
                else:
                    prev = toks[i - 1]
                    if prev != "." and prev != "->":
                        v = resolve(t)
                        if v is not None:
                            use(v, i)
            elif t == "(":
                prev = toks[i - 1] if i else ""
                flag = 1 if prev == "for" else (2 if prev in _CTRL else 0)
                parens.append([prev if prev[:1] in _IDSTART and prev not in KEYWORDS else None, 0, flag])
                stmt = flag == 1
            elif t == ")":
                if len(parens) > top[2]:
                    pe = parens.pop()
                    if pe[2] == 1:
                        for f in reversed(frames):
                            if f[0] == F_FOR and not f[3]:
                                f[3] = True
                                break
                    stmt = pe[2] != 0
            elif t == ",":
                if len(parens) > top[2]:
                    parens[-1][1] += 1
            elif t == ";":
                if len(parens) <= top[2]:
                    stmt = True
                    if top[0] == F_FOR:
                        self.close_fors(o)
            elif t == "{":
                pf = pending_fn.pop(i, None)
                if pf is not None:
                    frames.append([F_FN, pf[1], len(parens), False, pf[0]])
                    stmt = True
                elif self.init_brace(i, top):
                    frames.append([F_INIT, -1, len(parens), False, top[4]])
                else:
                    sc = self.new_scope("block", self.cur_scope(), top[4], o)
                    frames.append([F_BLOCK, sc, len(parens), False, top[4]])
                    stmt = True
            elif t == "}":
                if len(frames) > 1:
                    f = frames[-1]
                    while len(frames) > 1:
                        f = frames.pop()
                        self.end_frame(f, o)
                        if f[0] != F_FOR:
                            break
                    del parens[f[2]:]
                    self.close_fors(o)
                    stmt = f[0] != F_INIT
                else:
                    stmt = True
            elif t == ":" and colon:
                stmt, colon = True, False
            i += 1
        self.finish(self.model, self.text)

    def init_brace(self, i: int, top: list[Any]) -> bool:
        prev = self.toks[i - 1] if i else ""
        if prev == "=":
            return True
        if top[0] == F_INIT and prev in ("{", ",", "="):
            return True
        return prev == ")" and self.is_cast(self.match[i - 1], i - 1)

    def close_fors(self, off: int) -> None:
        frames = self.frames
        while len(frames) > 1 and frames[-1][0] == F_FOR and frames[-1][3]:
            self.end_frame(frames.pop(), off)

    @staticmethod
    def finish(model: dict[str, Any], text: str) -> None:
        """Spans still open at the end of the file end there."""
        lines = text.split("\n")
        last = [len(lines), max(1, len(lines[-1]))]
        for sc in model["scopes"]:
            if sc["span"][2] is None:
                sc["span"][2], sc["span"][3] = last
        for fn in model["functions"]:
            if fn["body"][2] is None:
                fn["body"][2], fn["body"][3] = last
                fn["line_e"] = last[0]


# ------------------------------------------------------------------------------------- lookup

def _fn_at(model: dict[str, Any], line: int, col: int) -> int | None:
    for idx, fn in enumerate(model.get("functions") or []):
        l1, c1, l2, c2 = fn["body"]
        if (l1, c1) <= (line, col) <= (l2, c2):
            return idx
    return None


def lookup(model: dict[str, Any], line: int, col: int, name: str | None = None) -> dict[str, Any] | None:
    """The variable or member at (line, col): the use or member token whose span `[col, col + len(name))` holds
    `col`; failing that, with `name`, the single use or member of that name on the line. Returns
    `{"kind": "var", "var", "access", "uses": [[line, col, access], ...]}` (every use of the variable),
    `{"kind": "member", "var", "path", "op", "access", "member_uses": [[line, col, path, access], ...]}` (the same
    variable and path in the same function), or None."""
    vars_ = model.get("vars") or []
    uses = model.get("uses") or []
    members = model.get("members") or []
    hit: tuple[str, list[Any]] | None = None
    named: list[tuple[str, list[Any]]] = []
    for u in uses:
        if u[1] == line:
            nm = vars_[u[0]]["name"]
            if u[2] <= col < u[2] + len(nm):
                hit = ("var", u)
                break
            if nm == name:
                named.append(("var", u))
    if hit is None:
        for m in members:
            if m[1] == line:
                field = m[3][-1]
                if m[2] <= col < m[2] + len(field):
                    hit = ("member", m)
                    break
                if field == name:
                    named.append(("member", m))
    if hit is None:
        if name is None or len(named) != 1:
            return None
        hit = named[0]
    kind, row = hit
    if kind == "var":
        v = row[0]
        return {"kind": "var", "var": v, "access": row[3], "uses": [[u[1], u[2], u[3]] for u in uses if u[0] == v]}
    v, path = row[0], row[3]
    fn = vars_[v]["fn"]
    if fn is None:
        fn = _fn_at(model, row[1], row[2])
    same = []
    for m in members:
        if m[0] == v and m[3] == path and (vars_[v]["fn"] is not None or _fn_at(model, m[1], m[2]) == fn):
            same.append([m[1], m[2], m[3], m[5]])
    return {"kind": "member", "var": v, "path": path, "op": row[4], "access": row[5], "member_uses": same}
