"""webapp/backend/services/scope_service.py - Local scope resolution of C variables, parameters and struct fields.

A name clicked or hovered in a `.c` / `.h` file at a position (line, byte column) resolves to its declaration in
that file before any name-only lookup: `parser/c_ast/c_scope.py` reads the file's blob (latin-1, so its columns
are the byte columns of `/fs/file` tokens) into a scope model, cached per blob, and `lookup()` finds the variable
or member chain at the position. The answer (ScopeInfo, webapp/API.md section 2.21, webapp/API_ENDPOINTS.md
section 4.3) adds what the model cannot know from the file alone, from the symbol definitions of the revision
ranked for the ARCH and the viewed file (`SymbolService.ranked_definitions`):

- `type_def`: the struct / union / enum / typedef definition of the variable's or field's type (`kind=type`
  ranking: `m_symbol_def` rows `C_structdecl` / `C_uniondecl` / `C_enumdecl` named by the bare tag, `C_SCtypedef`
  by the typedef name); builtins never resolve.
- `inferred`: for each assignment straight from a call, the callee's function definition, its head
  (`hover_service.extract_signature`) and `c_scope.parse_signature()`'s return type, compared with the declared
  type (`agrees`); an already assembled source index (`source_index.loaded_revision`) answers for callees without
  a readable definition; macros resolve to nothing and are skipped.
- `member`: for `x->a.b`, each step's field declaration found with `c_scope.find_member()` in the body of the
  previous step's struct (typedefs followed to their struct body), at most MAX_MEMBER_DEPTH steps.

`extern` declarations and names the model does not know return `{"found": false}`: the global resolution of the
existing endpoints answers for them.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable

from fastapi import HTTPException

from parser.c_ast.c_scope import extract_scopes, find_member, lookup, parse_signature, type_base
from parser.source_facts import RETURN_ANNOTATIONS, TOKEN, TYPE_QUALIFIERS
from webapp.backend.config import RepoScopedCache
from webapp.backend.security.git import is_safe_rev
from webapp.backend.security.jail import is_safe_rel_path
from webapp.backend.security.sql import is_valid_identifier
from webapp.backend.services.hover_service import extract_signature
from webapp.backend.services.symbol_service import TYPE_DEF_TYPES

logger = logging.getLogger(__name__)

C_SUFFIXES = (".c", ".h")
# Largest blob the scope model is built for (the largest v3.0 .c file is ~1 MB)
MAX_BLOB = 4 << 20
# Member steps resolved in one chain (`a->b.c->d`), typedef hops followed to a struct body
MAX_MEMBER_DEPTH = 8
MAX_TYPEDEF_DEPTH = 4
# Lines above a typedef definition searched for its `typedef` keyword when its row spans only the last line
TYPEDEF_LOOKBACK = 300
# Distinct callees whose return types one answer infers
MAX_INFERRED = 8
# Function definitions of one callee whose heads are read for its return type
MAX_CALLEE_DEFS = 3
FUNCTION_TYPES = frozenset(("C_functionproto", "C_functionprotodecl"))
PROTOTYPE_LINKAGE = 2

KIND_LABELS = {"param": "parameter", "local": "local variable", "static_local": "static local",
               "file": "file variable", "field": "struct field"}
ACCESS_BADGES = {"decl": "Decl", "r": "Read", "w": "Write", "rw": "ReadWrite", "addr": "AddressOf"}
_WRITES = ("w", "rw", "addr")
# Annotation words that never change what a type is (address space, sparse and section markers)
_TYPE_NOISE = (TYPE_QUALIFIERS | RETURN_ANNOTATIONS) - {"const", "volatile"}
_TYPEDEF_RE = re.compile(r"\btypedef\b")


def normalize_type(text: str | None) -> str:
    """Comparable form of a type text: whitespace, `__user` / `__iomem`-style annotations and `static` / `inline`
    words dropped, and the words of each pointer level sorted (`const char *` == `char const *`, `unsigned long` ==
    `long unsigned`)."""
    segs: list[tuple[list[str], list[str]]] = [([], [])]
    for tok in TOKEN.findall(text or ""):
        if tok == "*":
            segs.append(([], []))
        elif tok in _TYPE_NOISE:
            continue
        elif tok[:1].isalnum() or tok[:1] == "_":
            segs[-1][0].append(tok)
        else:
            segs[-1][1].append(tok)
    return "*".join(" ".join(sorted(words) + punct) for words, punct in segs)


def declaration_text(type_text: str | None, name: str) -> str:
    """`<type> <name>` as C writes it: `struct pci_dev *dev`, `int buf[4]`, `void (*fn)(int)`; the bare name
    without a type."""
    if not type_text:
        return name
    if "(*" in type_text:
        return type_text.replace("(*", "(*" + name, 1)
    if "[" in type_text:
        head, _, tail = type_text.partition("[")
        return f"{head.rstrip()} {name}[{tail}"
    return f"{type_text}{name}" if type_text.endswith("*") else f"{type_text} {name}"


def _type_ref(base: dict[str, Any] | None, pointer: int, array: bool) -> dict[str, Any] | None:
    if not base or not base.get("kind") or not base.get("name"):
        return None
    return {"kind": base["kind"], "name": base["name"], "pointer": int(pointer or 0), "array": bool(array)}


def _counts(uses: list[dict[str, Any]]) -> dict[str, int]:
    out = {"decl": 0, "r": 0, "w": 0, "rw": 0, "addr": 0}
    for u in uses:
        out[u["access"]] = out.get(u["access"], 0) + 1
    return out


def _functions_at(model: dict[str, Any]) -> list[tuple[tuple[int, int], tuple[int, int], dict[str, Any]]]:
    return [((f["body"][0], f["body"][1]), (f["body"][2], f["body"][3]), f) for f in model.get("functions") or []]


def function_at(model: dict[str, Any], line: int, col: int) -> dict[str, Any] | None:
    """The function whose body holds (line, col), else None."""
    for start, end, fn in _functions_at(model):
        if start <= (line, col) <= end:
            return fn
    return None


def _fn_info(fn: dict[str, Any] | None) -> dict[str, Any] | None:
    return {"name": fn["name"], "line_s": fn["line_s"], "line_e": fn["line_e"]} if fn else None


class ScopeService:
    """ScopeInfo of the token at a position of a C file (see the module docstring).

    `definitions_fn(version, name, arch, from_file, kind)` gives the ranked in-tree definitions
    (`SymbolService.ranked_definitions`); `read_source(version, path)` the text of a file at the revision. Both are
    replaceable (tests)."""

    def __init__(self, definitions_fn: Callable[..., list[dict[str, Any]]] | None = None,
                 read_source: Callable[[str, str], str | None] | None = None) -> None:
        self._definitions_fn = definitions_fn
        self._read_source = read_source
        self._models: RepoScopedCache = RepoScopedCache(maxsize=128)
        self._answers: RepoScopedCache = RepoScopedCache(maxsize=2048)

    # ------------------------------------------------------------------ sources

    @staticmethod
    def _commit(version: str) -> str:
        from webapp.backend.services.tree_resolver import git_tree_resolver
        return git_tree_resolver.resolve_commit_sha(version) or version

    def _blob(self, version: str, path: str) -> tuple[str, str] | None:
        """(blob key, latin-1 text) of path at the revision, or None (missing, a directory, too large)."""
        if self._read_source is not None:
            text = self._read_source(version, path)
            return (f"{version}:{path}", text) if text is not None else None
        from webapp.backend.services.git_reader import git_reader
        from webapp.backend.services.tree_resolver import git_tree_resolver
        tree = git_tree_resolver.list_tree(version)
        blob_sha = (tree or {}).get(path)
        if not blob_sha:
            return None
        cached = self._models.get(("text", blob_sha))
        if cached is not None:
            return blob_sha, cached
        _status, data, _sha = git_reader.read_object(blob_sha, "")
        if data is None or len(data) > MAX_BLOB:
            return None
        text = data.decode("latin-1")
        self._models[("text", blob_sha)] = text
        return blob_sha, text

    def _text(self, version: str, path: str) -> str | None:
        found = self._blob(version, path)
        return found[1] if found else None

    def model(self, version: str, path: str) -> dict[str, Any] | None:
        """The c_scope model of a `.c` / `.h` file at the revision (cached per blob), or None."""
        if not path.endswith(C_SUFFIXES):
            return None
        found = self._blob(version, path)
        if found is None:
            return None
        key, text = found
        model = self._models.get(("model", key))
        if model is None:
            model = extract_scopes(text)
            self._models[("model", key)] = model
        return model

    def _definitions(self, memo: dict, version: str, name: str, arch: str, from_file: str,
                     kind: str | None = None) -> list[dict[str, Any]]:
        key = ("defs", name, kind)
        if key in memo:
            return memo[key]
        fn = self._definitions_fn
        if fn is None:
            from webapp.backend.services.symbol_service import SymbolService
            fn = SymbolService().ranked_definitions
        try:
            rows = fn(version, name, arch, from_file, kind) if is_valid_identifier(name) else []
        except HTTPException:
            rows = []
        except Exception as exc:  # noqa: BLE001 - an unreachable index leaves the definition unresolved
            logger.debug("definitions of %s unavailable: %s", name, exc)
            rows = []
        memo[key] = rows
        return rows

    # ------------------------------------------------------------------ types

    def _type_def(self, memo: dict, version: str, arch: str, from_file: str,
                  ref: dict[str, Any] | None) -> dict[str, Any] | None:
        """{file, line, line_e, kind, name} of the definition of a struct / union / enum / typedef reference."""
        if not ref or ref.get("kind") not in TYPE_DEF_TYPES:
            return None
        wanted = TYPE_DEF_TYPES[ref["kind"]]
        rows = [r for r in self._definitions(memo, version, ref["name"], arch, from_file, "type")
                if r.get("type_name") == wanted and r.get("file")]
        # Bodies first: a prototype linkage or a one-line `struct foo;` forward declaration sorts last
        rows.sort(key=lambda r: (1 if r.get("linkage") == PROTOTYPE_LINKAGE else 0,
                                 1 if ref["kind"] in ("struct", "union") and (r.get("line_e") or 0) <= (r.get("line_s") or 0) else 0))
        if not rows:
            return None
        r = rows[0]
        return {"file": r["file"], "line": r["line_s"], "line_e": r.get("line_e") or r["line_s"],
                "kind": ref["kind"], "name": ref["name"]}

    def _struct_body(self, memo: dict, version: str, arch: str, from_file: str, ref: dict[str, Any] | None,
                     depth: int = 0) -> tuple[dict[str, Any], str, int, int] | None:
        """(definition, file text, line_s, line_e) of the struct / union body a type reference names; a typedef
        resolves to its own body (`typedef struct {...} foo_t;`) or to the type it names (`typedef struct foo
        foo_t;`)."""
        if not ref or ref.get("kind") not in ("struct", "union", "typedef") or depth > MAX_TYPEDEF_DEPTH:
            return None
        tdef = self._type_def(memo, version, arch, from_file, ref)
        if tdef is None:
            return None
        text = memo.get(("text", tdef["file"]))
        if text is None:
            text = self._text(version, tdef["file"]) or ""
            memo[("text", tdef["file"])] = text
        lines = text.split("\n")
        line_s, line_e = int(tdef["line"]), int(tdef["line_e"])
        if ref["kind"] != "typedef":
            return tdef, text, line_s, line_e
        region = "\n".join(lines[line_s - 1:line_e])
        if not _TYPEDEF_RE.search(region):
            for i in range(line_s - 1, max(0, line_s - 1 - TYPEDEF_LOOKBACK) - 1, -1):
                if _TYPEDEF_RE.search(lines[i]):
                    line_s = i + 1
                    region = "\n".join(lines[line_s - 1:line_e])
                    break
        if "{" in region:
            return tdef, text, line_s, line_e
        # `typedef struct foo foo_t;`: the type between `typedef` and the name
        m = re.search(r"\btypedef\b(.*?)\b" + re.escape(ref["name"]) + r"\s*;", region, re.S)
        if not m:
            return None
        tb = type_base(m.group(1))
        if not tb["kind"] or tb["name"] == ref["name"]:
            return None
        return self._struct_body(memo, version, arch, from_file, {"kind": tb["kind"], "name": tb["name"]}, depth + 1)

    def _member_chain(self, memo: dict, version: str, arch: str, from_file: str, base_ref: dict[str, Any] | None,
                      path: list[str]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
        """(struct holding the last field, field_def, field info) of a member path from the base variable's type."""
        ref = base_ref
        steps = [p for p in path if p != "[]"]
        if not steps or len(steps) > MAX_MEMBER_DEPTH:
            return None, None, None
        for i, step in enumerate(steps):
            body = self._struct_body(memo, version, arch, from_file, ref)
            if body is None:
                return None, None, None
            tdef, text, line_s, line_e = body
            fm = find_member(text, line_s, line_e, step)
            if fm is None:
                return {k: tdef[k] for k in ("kind", "name", "file", "line")}, None, None
            if i == len(steps) - 1:
                struct = {k: tdef[k] for k in ("kind", "name", "file", "line")}
                field_def = {"file": tdef["file"], "line": fm["line"], "col": fm["col"], "type": fm["type"]}
                return struct, field_def, fm
            ref = fm.get("base")
        return None, None, None

    # ------------------------------------------------------------------ inference

    def _callee_return(self, memo: dict, version: str, arch: str, from_file: str,
                       callee: str) -> tuple[str, str | None, int | None] | None:
        """(return type, file, line) of a function's definition, or None (a macro, an unknown name)."""
        rows = [r for r in self._definitions(memo, version, callee, arch, from_file)
                if r.get("type_name") in FUNCTION_TYPES and r.get("file")]
        for r in rows[:MAX_CALLEE_DEFS]:
            text = memo.get(("text", r["file"]))
            if text is None:
                text = self._text(version, r["file"]) or ""
                memo[("text", r["file"])] = text
            lines = text.split("\n")
            if not lines or not r.get("line_s"):
                continue
            sig = parse_signature(extract_signature(lines, int(r["line_s"]) - 1))
            if sig and sig["name"] == callee and sig["return_type"] and not sig["macro"]:
                return sig["return_type"], r["file"], int(r["line_s"])
        if rows:
            return None
        try:
            from webapp.backend.services import source_index
            idx = source_index.loaded_revision(self._commit(version))
            types = idx.return_type(callee) if idx is not None else []
        except Exception as exc:  # noqa: BLE001 - the source index is optional here
            logger.debug("source index return type of %s unavailable: %s", callee, exc)
            types = []
        return (types[0], None, None) if types else None

    def _inferred(self, memo: dict, version: str, arch: str, from_file: str, model: dict[str, Any], v: int,
                  declared: str | None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        callees: dict[str, Any] = {}
        for row in model.get("assigns") or []:
            if row[0] != v:
                continue
            callee = row[3]
            if callee not in callees:
                callees[callee] = (self._callee_return(memo, version, arch, from_file, callee)
                                   if len(callees) < MAX_INFERRED else None)
            found = callees[callee]
            if found is None:
                continue
            rtype, file, def_line = found
            out.append({"callee": callee, "line": row[1], "return_type": rtype,
                        "agrees": (normalize_type(rtype) == normalize_type(declared)) if declared else None,
                        "file": file, "def_line": def_line})
        return out

    # ------------------------------------------------------------------ resolution

    def resolve(self, version: str, path: str, line: int, col: int | None, name: str | None,
                arch: str) -> dict[str, Any]:
        """ScopeInfo of the token at (line, col) of `path`, or `{"found": false}`."""
        info, _in_fn = self.locate(version, path, line, col, name, arch)
        return info or {"found": False}

    def locate(self, version: str, path: str, line: int, col: int | None, name: str | None,
               arch: str) -> tuple[dict[str, Any] | None, bool]:
        """(ScopeInfo or None, whether the position lies inside a function body of the file)."""
        clean = str(path or "").strip().strip("/")
        if not clean or not is_safe_rel_path(clean) or not is_safe_rev(version) or not line or line < 1:
            return None, False
        if name is not None and not is_valid_identifier(name):
            return None, False
        col = int(col or 0)
        key = (self._commit(version) if self._read_source is None else version, clean, int(line), col, name or "",
               arch)
        hit = self._answers.get(key)
        if hit is not None:
            return hit
        model = self.model(version, clean)
        if model is None:
            return None, False
        answer = (self._build(version, clean, int(line), col, name, arch, model), function_at(model, line, col or 1) is not None)
        self._answers[key] = answer
        return answer

    def _build(self, version: str, path: str, line: int, col: int, name: str | None, arch: str,
               model: dict[str, Any]) -> dict[str, Any] | None:
        hit = lookup(model, line, col, name)
        vars_ = model.get("vars") or []
        if hit is not None and name:
            got = vars_[hit["var"]]["name"] if hit["kind"] == "var" else hit["path"][-1]
            if got != name:  # an inexact column on a neighbouring token: the single use of `name` on the line
                hit = lookup(model, line, 0, name)
        if hit is None:
            return None
        var = vars_[hit["var"]]
        if var.get("extern"):
            return None
        memo: dict = {}
        fn_index = var.get("fn")
        fns = model.get("functions") or []
        fn = fns[fn_index] if fn_index is not None and fn_index < len(fns) else function_at(model, line, col or 1)
        if hit["kind"] == "var":
            return self._var_info(memo, version, path, line, col, arch, model, hit, var, fn)
        return self._field_info(memo, version, path, line, col, arch, hit, var, fn)

    def _var_info(self, memo: dict, version: str, path: str, line: int, col: int, arch: str, model: dict[str, Any],
                  hit: dict[str, Any], var: dict[str, Any], fn: dict[str, Any] | None) -> dict[str, Any]:
        uses = [{"line": u[0], "col": u[1], "access": u[2]} for u in hit["uses"]]
        ref = _type_ref(var.get("base"), var.get("pointer"), var.get("array"))
        inferred = self._inferred(memo, version, arch, path, model, hit["var"], var.get("type"))
        effective = var.get("type") or (inferred[0]["return_type"] if inferred else None)
        if ref is None and not var.get("type") and effective:
            tb = type_base(effective)
            ref = _type_ref({"kind": tb["kind"], "name": tb["name"]}, tb["pointer"], tb["array"])
        writes = [u for u in uses if u["access"] in _WRITES or (u["access"] == "decl" and var.get("init"))]
        before = [u for u in writes if (u["line"], u["col"]) <= (line, col or 1 << 30)]
        type_def = self._type_def(memo, version, arch, path, ref)
        return {
            "found": True, "kind": var["kind"], "name": var["name"], "file": path, "type": var.get("type"),
            "type_ref": ref, "type_def": _public_def(type_def),
            "decl": {"line": var["line"], "col": var["col"]},
            "init": {"line": var["init"][0], "col": var["init"][1]} if var.get("init") else None,
            "function": _fn_info(fn), "access": hit["access"], "uses": uses, "counts": _counts(uses),
            "inferred": inferred, "effective_type": effective,
            "last_assign": {"line": before[-1]["line"], "col": before[-1]["col"]} if before else None,
            "member": None,
        }

    def _field_info(self, memo: dict, version: str, path: str, line: int, col: int, arch: str, hit: dict[str, Any],
                    var: dict[str, Any], fn: dict[str, Any] | None) -> dict[str, Any]:
        uses = [{"line": m[0], "col": m[1], "access": m[3]} for m in hit["member_uses"]]
        base_ref = _type_ref(var.get("base"), var.get("pointer"), var.get("array"))
        struct, field_def, fm = self._member_chain(memo, version, arch, path, base_ref, hit["path"])
        ref = _type_ref(fm.get("base"), fm.get("pointer"), fm.get("array")) if fm else None
        writes = [u for u in uses if u["access"] in _WRITES]
        before = [u for u in writes if (u["line"], u["col"]) <= (line, col or 1 << 30)]
        type_def = self._type_def(memo, version, arch, path, ref)
        return {
            "found": True, "kind": "field", "name": hit["path"][-1], "file": path,
            "type": field_def["type"] if field_def else None, "type_ref": ref, "type_def": _public_def(type_def),
            "decl": None, "init": None, "function": _fn_info(fn), "access": hit["access"], "uses": uses,
            "counts": _counts(uses), "inferred": [], "effective_type": field_def["type"] if field_def else None,
            "last_assign": {"line": before[-1]["line"], "col": before[-1]["col"]} if before else None,
            "member": {
                "base": var["name"], "base_kind": var["kind"], "base_type": var.get("type"),
                "base_decl": {"line": var["line"], "col": var["col"]}, "path": list(hit["path"]), "op": hit["op"],
                "struct": struct, "field_def": field_def,
            },
        }


def _public_def(tdef: dict[str, Any] | None) -> dict[str, Any] | None:
    if tdef is None:
        return None
    return {"file": tdef["file"], "line": tdef["line"], "line_e": tdef["line_e"], "kind": tdef["kind"],
            "name": tdef["name"]}


# ---------------------------------------------------------------------- endpoint payloads

def _declaration_site(info: dict[str, Any]) -> tuple[str | None, int | None]:
    if info["kind"] == "field":
        fd = (info.get("member") or {}).get("field_def")
        return (fd["file"], fd["line"]) if fd else (None, None)
    return info["file"], info["decl"]["line"]


def hover_payload(version: str, name: str, arch: str, info: dict[str, Any]) -> dict[str, Any]:
    """The hover card of a locally resolved name (`kind` the ScopeInfo kind, `signature` `<type> <name>`)."""
    file, line = _declaration_site(info)
    return {"version": version, "name": name, "arch": arch, "found": True, "kind": info["kind"],
            "signature": declaration_text(info.get("type") or info.get("effective_type"), info["name"]),
            "file": file, "line": line, "definitions": 1, "doc": None, "scope": info}


def detail_payload(version: str, name: str, arch: str, from_file: str, info: dict[str, Any]) -> dict[str, Any] | None:
    """The symbol detail of a locally resolved name (the declaration as its one definition); None for a field
    whose declaration was not found (the global lookup answers)."""
    file, line = _declaration_site(info)
    if not file:
        return None
    label = KIND_LABELS.get(info["kind"], info["kind"])
    definition = {"file": file, "line_s": line, "line_e": line, "type_name": label}
    return {"version": version, "name": name, "symbol_name": name, "symbol": name, "type_name": label,
            "file_path": file, "line": line, "line_s": line, "definition": definition, "definitions": [definition],
            "arch": arch, "from_file": from_file, "scope": info}


def xref_payload(version: str, name: str, arch: str, limit: int | None, model: dict[str, Any] | None,
                 info: dict[str, Any]) -> dict[str, Any]:
    """The cross-references of a locally resolved name: its declaration and its uses in scope."""
    file, line = _declaration_site(info)
    label = KIND_LABELS.get(info["kind"], info["kind"])
    definition = {"file": file, "line_s": line, "line_e": line, "type_name": label, "ast_id": None} if file else None
    spans = _functions_at(model or {})

    def caller(ln: int, cl: int) -> str | None:
        return next((f["name"] for s, e, f in spans if s <= (ln, cl) <= e), None)

    uses = info["uses"][:limit] if limit and limit > 0 else info["uses"]
    references = [{"file": info["file"], "caller": caller(u["line"], u["col"]), "fid": None, "line": u["line"],
                   "char_s": u["col"], "tag_id": None, "type": ACCESS_BADGES.get(u["access"], "Ref"),
                   "access": u["access"]} for u in uses]
    return {"version": version, "arch": arch, "symbol": name, "name": name, "ast_id": None,
            "definitions": [definition] if definition else [], "definition": definition,
            "definition_unindexed": False, "references": references, "scope": info}


scope_service = ScopeService()
