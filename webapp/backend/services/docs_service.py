"""webapp/backend/services/docs_service.py - Documentation Cross-References & kernel-doc Extraction.

1. Label index: every `.. _label:` hyperlink target under Documentation/ at a revision, with the
   title of the section it precedes, so `:ref:` roles resolve across documents.
2. kernel-doc: parses `/** ... */` kernel-doc comments of a source file at a revision into
   structured entries (functions, structs/unions/enums, typedefs, macros, DOC: sections),
   honouring the `.. kernel-doc::` directive selectors (:export:, :internal:, :functions:,
   :identifiers:, :no-identifiers:, :doc:).
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
from collections import OrderedDict
from typing import Any

from fastapi import HTTPException

from webapp.backend.config import get_repo_dir
from webapp.backend.database.helpers import get_version_info
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.security.jail import is_safe_rel_path
from webapp.backend.services.git_reader import git_reader
from webapp.backend.services.tree_resolver import git_tree_resolver

logger = logging.getLogger(__name__)

ADORNMENT_RE = re.compile(r"""^([=\-`:.'"~^_*+#<>!$%&,;?@\[\]\\/|{}()])\1{2,}\s*$""")
LABEL_RE = re.compile(r"^\.\.\s+_([^:`][^:]*|`[^`]+`):\s*$")

KDOC_START_RE = re.compile(r"^\s*/\*\*\s*$")
KDOC_HEAD_RE = re.compile(
    r"^\s*(?:(?P<kind>struct|union|enum|typedef)\s+)?(?P<name>[A-Za-z_][\w]*)\s*(?:\(\s*\))?\s*(?:-+\s*(?P<brief>.*))?$"
)
KDOC_DOC_RE = re.compile(r"^\s*DOC:\s*(?P<title>.*)$")
KDOC_PARAM_RE = re.compile(r"^@(?P<name>[\w.]+(?:\.\.\.)?|\.\.\.)\s*:\s*(?P<desc>.*)$")
KDOC_SECTION_RE = re.compile(
    r"^(?P<title>Return|Returns|Context|Note|Notes|Example|Examples|Description|Locking|Warning|See also|Lock|Caller|Callers)\s*:\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
EXPORT_RE = re.compile(r"\bEXPORT_(?:SYMBOL|SYMBOL_GPL|SYMBOL_NS|SYMBOL_NS_GPL|SYMBOL_GPL_FUTURE|SYMBOL_FOR_MODULES)\s*\(\s*(\w+)")


class DocsService:
    """Documentation helpers backed by the Git object store of the active repository."""

    def __init__(self, max_cache_size: int = 8) -> None:
        self._label_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._max_cache_size = max_cache_size

    # ------------------------------------------------------------------
    # Label index
    # ------------------------------------------------------------------
    def get_label_index(self, version_name: str) -> dict[str, Any]:
        """Map every Documentation/ `.. _label:` target to {path, title} at the revision."""
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
        if vid is None:
            raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")
        sha = git_tree_resolver.resolve_commit_sha(vname)
        if sha is None:
            raise HTTPException(status_code=404, detail=f"Revision '{vname}' is not available in Git.")

        cache_key = f"{get_repo_dir()}:{sha}"
        with self._lock:
            if cache_key in self._label_cache:
                self._label_cache.move_to_end(cache_key)
                return self._label_cache[cache_key]

        try:
            out = subprocess.run(
                ["git", "-C", str(get_repo_dir()), "grep", "-I", "-n", "-A6", "--full-name", "-E",
                 # Pathspec wildcards match across "/", so this covers every .rst below Documentation/
                 r"^\.\.[[:space:]]+_[^:]+:[[:space:]]*$", sha, "--", "Documentation/*.rst"],
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise HTTPException(status_code=500, detail=f"git grep failed: {e}") from e

        labels: dict[str, dict[str, Any]] = {}
        groups = out.stdout.decode("utf-8", errors="replace").split("\n--\n")
        line_re = re.compile(r"^[0-9a-f]{40}:(?P<path>[^:\n]+?)(?P<sep>[:-])(?P<line>\d+)(?P=sep)(?P<text>.*)$")
        for group in groups:
            current: list[tuple[str, int, str, bool]] = []
            for raw in group.splitlines():
                m = line_re.match(raw)
                if m:
                    current.append((m["path"], int(m["line"]), m["text"], m["sep"] == ":"))
            for idx, (path, line_no, text, is_match) in enumerate(current):
                lm = LABEL_RE.match(text) if is_match else None
                if not lm:
                    continue
                label = lm.group(1).strip("`").strip().lower()
                title = self._title_after([t for p, _, t, _ in current[idx + 1:] if p == path])
                labels.setdefault(label, {"path": path, "line": line_no, "title": title})

        result = {"version": vname, "total": len(labels), "labels": labels}
        with self._lock:
            self._label_cache[cache_key] = result
            if len(self._label_cache) > self._max_cache_size:
                self._label_cache.popitem(last=False)
        return result

    @staticmethod
    def _title_after(lines: list[str]) -> str:
        """Section title following a label (skips blank lines, further labels, and overlines)."""
        rest = lines
        i = 0
        while i < len(rest) and (not rest[i].strip() or LABEL_RE.match(rest[i])):
            i += 1
        if i < len(rest) and ADORNMENT_RE.match(rest[i]):
            i += 1
        if i + 1 < len(rest) and rest[i].strip() and ADORNMENT_RE.match(rest[i + 1]):
            return rest[i].strip()
        return ""

    # ------------------------------------------------------------------
    # kernel-doc
    # ------------------------------------------------------------------
    def get_kernel_doc(
        self,
        version_name: str,
        path: str,
        export: bool = False,
        internal: bool = False,
        functions: str = "",
        doc: str = "",
        no_identifiers: str = "",
    ) -> dict[str, Any]:
        """Extract kernel-doc entries of `path` at the revision, filtered like the Sphinx directive."""
        norm_path = path.strip().strip("/")
        if not norm_path or not is_safe_rel_path(norm_path):
            raise HTTPException(status_code=400, detail="A valid relative file path must be specified.")
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
        if vid is None:
            raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")
        raw, is_dir, _ = git_reader.read_file(vname, norm_path)
        if raw is None or is_dir:
            raise HTTPException(status_code=404, detail=f"'{norm_path}' not found at '{vname}'.")
        source = raw.decode("utf-8", errors="replace")

        entries = parse_kernel_doc(source)
        exported = set(EXPORT_RE.findall(source))
        wanted = {n for n in re.split(r"[\s,]+", functions.strip()) if n}
        excluded = {n for n in re.split(r"[\s,]+", no_identifiers.strip()) if n}

        selected = []
        for e in entries:
            if doc:
                if e["kind"] == "doc" and e["name"].strip().lower() == doc.strip().lower():
                    selected.append(e)
                continue
            if e["kind"] == "doc" and (wanted or export or internal):
                continue
            if wanted and e["name"] not in wanted:
                continue
            if excluded and e["name"] in excluded:
                continue
            # :export: documents only EXPORT_SYMBOL'd identifiers; :internal: everything else
            if export and e["name"] not in exported:
                continue
            if internal and e["name"] in exported:
                continue
            selected.append(e)

        return {
            "version": vname,
            "path": norm_path,
            "total": len(selected),
            "exported": sorted(exported),
            "entries": selected,
        }


def _strip_comment_line(line: str) -> str:
    s = line.rstrip()
    m = re.match(r"^\s*\*(?!/)\s?(.*)$", s)
    return m.group(1) if m else s.strip()


def _declaration_after(lines: list[str], start: int) -> tuple[str, int]:
    """The C declaration following a kernel-doc comment, up to its body/terminator."""
    decl: list[str] = []
    depth = 0
    i = start
    while i < len(lines) and len(decl) < 200:
        line = lines[i]
        stripped = line.strip()
        if not decl and not stripped:
            i += 1
            continue
        decl.append(line.rstrip())
        if stripped.startswith("#define"):
            while decl[-1].endswith("\\") and i + 1 < len(lines):
                i += 1
                decl.append(lines[i].rstrip())
            break
        depth += line.count("{") - line.count("}")
        if "{" in line and re.match(r"^\s*(static\s+|inline\s+|extern\s+|__\w+\s+)*[\w\s\*]+\(", decl[0]) and ")" in "".join(decl):
            # Function definition: stop at the opening brace
            decl[-1] = decl[-1].split("{", 1)[0].rstrip()
            break
        if depth <= 0 and stripped.endswith(";"):
            break
        i += 1
    return "\n".join(decl).strip(), i


def parse_kernel_doc(source: str) -> list[dict[str, Any]]:
    """Parse kernel-doc comments (Documentation/doc-guide/kernel-doc.rst) into structured entries."""
    lines = source.splitlines()
    entries: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        if not KDOC_START_RE.match(lines[i]):
            i += 1
            continue
        start_line = i + 1
        body: list[str] = []
        i += 1
        while i < len(lines) and "*/" not in lines[i]:
            body.append(_strip_comment_line(lines[i]))
            i += 1
        i += 1  # skip closing */
        if not body:
            continue

        head = body[0].strip()
        doc_m = KDOC_DOC_RE.match(head)
        if doc_m:
            entries.append({
                "kind": "doc",
                "name": doc_m["title"].strip(),
                "brief": "",
                "signature": "",
                "params": [],
                "sections": [{"title": "", "body": "\n".join(body[1:]).strip("\n")}],
                "line": start_line,
            })
            continue

        head_m = KDOC_HEAD_RE.match(head)
        if not head_m:
            continue
        kind = head_m["kind"] or "function"
        name = head_m["name"]
        brief_lines = [head_m["brief"] or ""]

        params: list[dict[str, str]] = []
        sections: list[dict[str, Any]] = []
        current_param: dict[str, str] | None = None
        current_section: dict[str, Any] | None = None
        in_brief = True
        for raw in body[1:]:
            text = raw.rstrip()
            pm = KDOC_PARAM_RE.match(text.strip())
            sm = KDOC_SECTION_RE.match(text.strip())
            if pm and current_section is None:
                current_param = {"name": pm["name"], "desc": pm["desc"].strip()}
                params.append(current_param)
                in_brief = False
                continue
            if sm:
                title = sm["title"].capitalize()
                if title == "Returns":
                    title = "Return"
                current_section = {"title": title, "lines": [sm["rest"].strip()] if sm["rest"].strip() else []}
                sections.append(current_section)
                current_param = None
                in_brief = False
                continue
            if not text.strip():
                if in_brief:
                    in_brief = False
                elif current_param is not None:
                    current_param = None
                if current_section is not None:
                    current_section["lines"].append("")
                continue
            if in_brief:
                brief_lines.append(text.strip())
            elif current_param is not None:
                current_param["desc"] += " " + text.strip()
            else:
                if current_section is None:
                    current_section = {"title": "Description", "lines": []}
                    sections.append(current_section)
                current_section["lines"].append(text)

        declaration, _ = _declaration_after(lines, i)
        if declaration.lstrip().startswith("#define"):
            kind = "macro" if head_m["kind"] is None else kind
        elif head_m["kind"] is None and re.match(r"^\s*(typedef)\b", declaration):
            kind = "typedef"

        for sec in sections:
            sec["body"] = "\n".join(sec.pop("lines")).strip("\n")
        entries.append({
            "kind": kind,
            "name": name,
            "brief": " ".join(b for b in brief_lines if b).strip(),
            "signature": declaration,
            "params": params,
            "sections": [s for s in sections if s["body"] or s["title"] != "Description"],
            "line": start_line,
        })
    return entries
