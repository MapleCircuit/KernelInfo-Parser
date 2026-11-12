"""parser/man_ast/man_facts.py - facts of a man page blob (`m_blob_facts` family 6, pure).

`extract(text)` derives a compact record from the document model of `parser/man_ast/man_parser.py`:
NAME aliases and summary, `.TH` fields, `.so` alias target, cross references (SEE ALSO first), and the C
declarations of the SYNOPSIS (function names, `#include` headers, struct/type/constant identifiers).
"""
from __future__ import annotations

import json
import re
import zlib
from typing import Any

from parser.man_ast.man_parser import parse, plain_text, walk_blocks
from parser.man_ast.man_paths import page_of_path

FAMILY = 6
FORMAT = 1

C_KEYWORDS = {
    "auto", "break", "case", "char", "const", "continue", "default", "do", "double", "else", "enum", "extern",
    "float", "for", "goto", "if", "inline", "int", "long", "register", "restrict", "return", "short",
    "signed", "sizeof", "static", "struct", "switch", "typedef", "union", "unsigned", "void", "volatile",
    "while", "_Bool", "bool", "_Complex", "_Imaginary", "_Atomic", "_Alignas", "_Alignof", "alignas",
    "alignof", "_Noreturn", "noreturn", "_Nullable", "_Nonnull", "_Null_unspecified", "typeof",
    "__typeof__", "typeof_unqual", "_Countof", "__attribute__", "__restrict", "__restrict__", "__inline",
    "__extension__", "__THROW", "__wur", "__const", "__signed", "__volatile", "__asm__", "asm",
    "_Static_assert", "static_assert", "_Generic", "_Thread_local", "thread_local", "constexpr",
    "nullptr", "true", "false", "NULL", "va_arg", "deprecated", "nodiscard", "maybe_unused", "unsequenced",
    "reproducible", "fallthrough",
}
_PROSE = {"the", "a", "an", "for", "of", "to", "and", "or", "in", "see", "is", "use", "with", "by", "no",
          "not", "on", "this", "that", "are", "be", "as", "it", "glibc", "since", "note", "from", "also"}
_COMMAND_SECTIONS = ("1", "6", "8")

XREF_RE = re.compile(r"(?<![\w.+:\-])([A-Za-z_][\w.+:\-]*)\((\d[A-Za-z]*)\)")
INCLUDE_RE = re.compile(r"#\s*include\s*[<\"]([^>\"]+)[>\"]")
DEFINE_RE = re.compile(r"#\s*define\s+([A-Za-z_]\w*)")
TAGGED_RE = re.compile(r"\b(?:struct|union|enum)\s+([A-Za-z_]\w*)")
TYPE_T_RE = re.compile(r"\b([A-Za-z_]\w*_t)\b")
CAPS_RE = re.compile(r"\b([A-Z][A-Z0-9]*(?:_\w+)?)\b")
IDENT_RE = re.compile(r"[A-Za-z_]\w*")
FEATURE_MACRO_RE = re.compile(r"^_(?:[A-Z0-9]+_)*SOURCE$|^_(?:FILE_OFFSET|TIME)_BITS$|^_REENTRANT$|^_THREAD_SAFE$")


def applies(path: str) -> bool:
    return page_of_path(path) is not None


def _blocks_text(blocks: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for b in blocks:
        t = b.get("t")
        if t == "p":
            out.append(plain_text(b["c"]))
        elif t == "pre":
            out.extend(plain_text(ln) for ln in b["lines"])
        elif t == "tp":
            out.extend(plain_text(tg) for tg in b["tags"])
            out.extend(_blocks_text(b["body"]))
        elif t in ("ip", "rs"):
            out.extend(_blocks_text(b["body"]))
        elif t == "ss":
            out.append(b.get("title", ""))
        elif t == "table":
            for row in b["rows"]:
                out.append("\t".join(plain_text(c) for c in row))
    return out


def section_text(section: dict[str, Any]) -> str:
    """Plain text of a document section (one block line per text line, no-break spaces as spaces)."""
    return "\n".join(_blocks_text(section.get("blocks", []))).replace(" ", " ")


def _find(doc: dict[str, Any], *titles: str) -> dict[str, Any] | None:
    for s in doc.get("sections", []):
        if s.get("title", "").strip().upper() in titles:
            return s
    return None


def _strip_parens(s: str) -> str:
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"\([^()]*\)", " ", s)
    return s


def _c_facts(text: str) -> tuple[list[str], list[str]]:
    """(function names, other identifiers) declared in C SYNOPSIS text."""
    fns: list[str] = []
    ids: list[str] = []
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"/\*.*", " ", text)
    text = re.sub(r"//[^\n]*", " ", text)
    text = re.sub(r'"(?:\\.|[^"\\\n])*"', " ", text)
    code_lines = []
    for ln in text.split("\n"):
        st = ln.strip()
        if st.startswith("#"):
            m = DEFINE_RE.match(st)
            if m:
                ids.append(m.group(1))
            continue
        if " " in st and re.search(r"[A-Za-z0-9)\]]\.$", st):
            continue  # prose sentence ("No glibc wrapper is provided ...; see NOTES.")
        code_lines.append(ln)
    code = "\n".join(code_lines)
    code = re.sub(r"\[\[[^\]]*\]\]", " ", code)
    # GNU forward parameter declarations: `read(size_t count; int fd, ...)` -> `read(int fd, ...)`
    code = re.sub(r"\(((?:[^();{}]*;)+)", "(", code)
    code = re.sub(r"__attribute__\s*\(\((?:[^()]|\([^()]*\))*\)\)", " ", code)
    for stmt in re.split(r"[;{}]", code):
        s = " ".join(stmt.split())
        if not s:
            continue
        for m in TAGGED_RE.finditer(s):
            ids.append(m.group(1))
        if s.startswith("typedef "):
            m = re.search(r"\(\s*\*\s*([A-Za-z_]\w*)\s*\)", s)
            if m:
                ids.append(m.group(1))
            else:
                names = IDENT_RE.findall(_strip_parens(s))
                if names and names[-1] not in C_KEYWORDS:
                    ids.append(names[-1])
        else:
            idx = s.find("(")
            prefix = s[:idx] if idx > 0 else ""
            m_assign = re.fullmatch(r"\s*[A-Za-z_]\w*\s*=\s*([A-Za-z_]\w*)\s*", prefix)
            if m_assign:
                # usage form in SYNOPSIS: `sockfd = socket(int domain, ...)`
                if m_assign.group(1) not in C_KEYWORDS:
                    fns.append(m_assign.group(1))
                prefix = ""
            if prefix and re.fullmatch(r"[\w\s*\[\]]*", prefix):
                m = re.search(r"([A-Za-z_]\w*)\s*$", prefix)
                words = prefix[:m.start()] if m else prefix
                before = words.replace("*", " ").split()
                plausible = before and len(before) <= 6 and not any(w.lower() in _PROSE for w in before)
                # function returning a function pointer: `void (*signal(int, ...))(int)`
                m2 = re.match(r"\(\s*\*\s*([A-Za-z_]\w*)\s*\(", s[idx:])
                if m2:
                    words = prefix.replace("*", " ").split()
                    if words and len(words) <= 6 and not any(w.lower() in _PROSE for w in words):
                        fns.append(m2.group(1))
                elif m and plausible and m.group(1) not in C_KEYWORDS:
                    m_sys = re.match(r"\(\s*SYS_([A-Za-z_]\w*)", s[idx:]) if m.group(1) == "syscall" else None
                    # `long syscall(SYS_foo, ...)`: the declared interface is the system call `foo`
                    fns.append(m_sys.group(1) if m_sys else m.group(1))
        if sum(1 for w in re.findall(r"[A-Za-z]+", s) if w in _PROSE) >= 2:
            continue  # prose sentence (e.g. a "Note: ..." paragraph), not a declaration
        for m in TYPE_T_RE.finditer(s):
            ids.append(m.group(1))
        for m in CAPS_RE.finditer(s):
            w = m.group(1)
            if len(w) >= 2 and w not in C_KEYWORDS:
                ids.append(w)
    fns = _dedupe(fns)
    fset = set(fns)
    ids = [i for i in _dedupe(ids) if i not in fset and i not in C_KEYWORDS and not FEATURE_MACRO_RE.match(i)]
    return fns, ids


def _dedupe(items: list[Any]) -> list[Any]:
    seen: set[Any] = set()
    out = []
    for it in items:
        key = tuple(it) if isinstance(it, list) else it
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _refs(blocks: list[dict[str, Any]]) -> list[list[str]]:
    out: list[list[str]] = []

    def runs_refs(runs: list[dict[str, Any]]) -> None:
        for r in runs:
            if "ref" in r:
                out.append([str(r["ref"][0]), str(r["ref"][1])])

    for b in walk_blocks(blocks):
        t = b.get("t")
        if t == "p":
            runs_refs(b["c"])
        elif t == "pre":
            for ln in b["lines"]:
                runs_refs(ln)
        elif t == "tp":
            for tg in b["tags"]:
                runs_refs(tg)
        elif t == "table":
            for row in b["rows"]:
                for c in row:
                    runs_refs(c)
    return out


def _names_summary(doc: dict[str, Any]) -> tuple[list[str], str]:
    sec = _find(doc, "NAME", "NOMBRE", "NOM", "BEZEICHNUNG")
    if sec is None:
        return [], ""
    text = " ".join(section_text(sec).split())
    m = re.search(r"\s+(?:-|—|–|--)\s+", text)
    if m:
        left, summary = text[:m.start()], text[m.end():]
    elif text.endswith((" -", " —")):
        left, summary = text[:-2], ""
    else:
        left, summary = text, ""
    names = [n.strip().removesuffix("()") for n in re.split(r",\s*|\s+", left)]
    names = [n for n in names if n]
    return _dedupe(names), summary.strip()


def extract(text: str) -> dict[str, Any] | None:
    if not text or not text.strip():
        return None
    doc = parse(text)
    th = doc.get("th") or {}
    rec: dict[str, Any] = {
        "n": [], "s": "",
        "th": [th.get("title", ""), th.get("section", ""), th.get("date", ""), th.get("source", ""),
               th.get("manual", "")],
        "so": doc.get("so") or "", "x": [], "fn": [], "hd": [], "id": [],
    }
    if doc.get("so"):
        return rec
    rec["n"], rec["s"] = _names_summary(doc)

    see = _find(doc, "SEE ALSO")
    xrefs: list[list[str]] = []
    if see is not None:
        for m in XREF_RE.finditer(section_text(see)):
            xrefs.append([m.group(1), m.group(2)])
        xrefs.extend(_refs(see["blocks"]))
    for s in doc.get("sections", []):
        if s is not see:
            xrefs.extend(_refs(s["blocks"]))
    rec["x"] = _dedupe(xrefs)

    syn = _find(doc, "SYNOPSIS", "SYNOPSES")
    if syn is not None:
        stext = section_text(syn)
        rec["hd"] = _dedupe(INCLUDE_RE.findall(stext))
        cut = re.search(r"Feature\s+Test\s+Macro", stext)
        if cut:
            stext = stext[:cut.start()]
        if not str(th.get("section", "")).startswith(_COMMAND_SECTIONS):
            rec["fn"], rec["id"] = _c_facts(stext)
    return rec


def encode(obj: dict[str, Any] | None) -> bytes:
    if not obj:
        return b""
    return zlib.compress(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode(), 3)


def decode(data: bytes | bytearray | memoryview | None) -> dict[str, Any] | None:
    if data is None:
        return None
    raw = bytes(data)
    if not raw:
        return None
    try:
        obj = json.loads(zlib.decompress(raw))
    except (zlib.error, ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None
