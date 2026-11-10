"""webapp/backend/services/hover_service.py - Hover cards: signature and kernel-doc of a symbol (G04, webapp/API.md section 4.3).

The definition is the one `GET /symbols/{v}/detail/{name}` (Go to Definition) ranks first for the ARCH and the
file the symbol is used in; its first lines up to the body (or the end of the declaration or macro) are the
signature, and the kernel-doc comment directly above it gives the summary and the parameters.
"""
from __future__ import annotations

import re
from typing import Any

MAX_SIG_LINES = 6
MAX_DOC_LINES = 120
_PARAM_RE = re.compile(r"^@(\w+|\.\.\.)\s*:\s*(.*)$")


def extract_signature(lines: list[str], start: int) -> str:
    """Lines from `start` (0-based) up to the body's `{`, a `;`, or the end of a macro; at most 6 lines."""
    out: list[str] = []
    for i in range(start, min(len(lines), start + MAX_SIG_LINES)):
        line = lines[i].rstrip("\n")
        if i == start and re.match(r"^\s*#\s*define\b", line) or (out and out[0].lstrip().startswith("#")):
            out.append(line)
            if not line.rstrip().endswith("\\"):
                break
            continue
        brace = line.find("{")
        semi = line.find(";")
        if brace >= 0 and (semi < 0 or brace < semi):
            head = line[:brace].rstrip()
            # `struct foo {` keeps the brace so the kind stays readable; a function body does not
            if re.search(r"\b(struct|union|enum|impl|trait)\b", head):
                out.append(line[:brace + 1].rstrip() + " … }")
            elif re.search(r"\bfn\b", head) or head.strip().endswith(")") or re.search(r"\)\s*(?:->\s*[^;{]+)?$", head):
                if head.strip():
                    out.append(head)
            elif not out and not head.strip().endswith(")"):
                out.append(line[:brace + 1].rstrip() + " … }")
            elif head.strip():
                out.append(head)
            break
        out.append(line)
        if semi >= 0:
            break
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def extract_doc(lines: list[str], start: int) -> dict[str, Any] | None:
    """The kernel-doc comment (`/** … */`) or Rust doc comment (`/// …`) ending right above `start`."""
    i = start - 1
    while i >= 0 and not lines[i].strip():
        i -= 1
    if i < 0:
        return None

    # Rust /// doc comment
    if lines[i].strip().startswith("///"):
        end = i
        while i >= 0 and end - i < MAX_DOC_LINES and lines[i].strip().startswith("///"):
            i -= 1
        start_doc = i + 1
        body = []
        for raw in lines[start_doc:end + 1]:
            t = raw.strip()
            t = re.sub(r"^///\s?", "", t)
            body.append(t)
        while body and not body[0]:
            body.pop(0)
        if not body:
            return None
        summary_head = body[0]
        params: list[dict[str, str]] = []
        paragraphs: list[str] = []
        cur: list[str] = []
        for line in body[1:]:
            pm = _PARAM_RE.match(line)
            if pm:
                params.append({"name": pm.group(1), "text": pm.group(2).strip()})
                continue
            if not line:
                if cur:
                    paragraphs.append(" ".join(cur))
                    cur = []
                continue
            cur.append(line.strip())
        if cur:
            paragraphs.append(" ".join(cur))
        return {"summary": summary_head, "description": paragraphs[0] if paragraphs else "", "params": params[:12],
                "more": len(paragraphs) > 1 or len(params) > 12, "line": start_doc + 1}

    # C kernel-doc comment (/** … */)
    if not lines[i].strip().endswith("*/"):
        return None
    end = i
    while i >= 0 and end - i < MAX_DOC_LINES and "/**" not in lines[i]:
        if "/*" in lines[i] and "/**" not in lines[i]:
            return None  # a plain comment, not kernel-doc
        i -= 1
    if i < 0 or "/**" not in lines[i]:
        return None
    body = []
    for raw in lines[i:end + 1]:
        t = raw.strip()
        t = re.sub(r"^/\*\*\s?", "", t)
        t = re.sub(r"\*/$", "", t).rstrip()
        t = re.sub(r"^\*\s?", "", t)
        body.append(t)
    while body and not body[0]:
        body.pop(0)
    if not body:
        return None
    title = body[0]
    m = re.match(r"^(?:(?:struct|union|enum|typedef)\s+)?[\w()]+\s*-\s*(.*)$", title)
    summary_head = m.group(1).strip() if m else title
    params = []
    paragraphs = []
    cur = []
    for line in body[1:]:
        pm = _PARAM_RE.match(line)
        if pm:
            params.append({"name": pm.group(1), "text": pm.group(2).strip()})
            continue
        if params and line and not cur and not paragraphs and line.startswith((" ", "\t")):
            params[-1]["text"] += " " + line.strip()
            continue
        if not line:
            if cur:
                paragraphs.append(" ".join(cur))
                cur = []
            continue
        cur.append(line.strip())
    if cur:
        paragraphs.append(" ".join(cur))
    return {"summary": summary_head, "description": paragraphs[0] if paragraphs else "", "params": params[:12],
            "more": len(paragraphs) > 1 or len(params) > 12, "line": i + 1}


class HoverService:
    def __init__(self) -> None:
        from webapp.backend.config import RepoScopedCache
        self._cache: RepoScopedCache = RepoScopedCache(maxsize=4096)

    def hover(self, version: str, name: str, arch: str, from_file: str | None, detail_fn) -> dict[str, Any]:
        from webapp.backend.services.git_reader import git_reader
        from webapp.backend.services.tree_resolver import git_tree_resolver
        sha = git_tree_resolver.resolve_commit_sha(version) or version
        key = (sha, name, arch, from_file or "")
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        from fastapi import HTTPException
        try:
            detail = detail_fn(version, name, arch, from_file)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            detail = {}
        out: dict[str, Any] = {"version": version, "name": name, "arch": arch, "found": bool(detail.get("file_path"))}
        if detail.get("is_kconfig"):
            k = detail.get("kconfig") or {}
            help_text = (k.get("help") or "").strip()
            selected_by = [str(d) for d in (k.get("selected_by") or [])][:12]
            implied_by = [str(d) for d in (k.get("implied_by") or [])][:12]
            if not selected_by and not implied_by:
                clean_sym = k.get("name") or (name[7:] if name.startswith("CONFIG_") else name)
                try:
                    from webapp.backend.services import kconfig_facts
                    sb, ib = kconfig_facts.reverse_dependencies(clean_sym)
                    selected_by = sb[:12]
                    implied_by = ib[:12]
                except Exception:
                    pass
                if not selected_by and not implied_by:
                    try:
                        from webapp.backend.services.kconfig_service import kconfig_service
                        graph = kconfig_service.get_graph(version, clean_sym, arch=arch)
                        for edge in graph.get("edges", []):
                            if edge.get("to") == clean_sym:
                                if edge.get("type") == "selects":
                                    selected_by.append(edge["from"])
                                elif edge.get("type") == "implies":
                                    implied_by.append(edge["from"])
                        selected_by = list(dict.fromkeys(selected_by))[:12]
                        implied_by = list(dict.fromkeys(implied_by))[:12]
                    except Exception:
                        pass
            out.update(kind="kconfig", file=detail.get("file_path"), line=detail.get("line_s"),
                       kconfig={"name": k.get("name"), "type": k.get("type_name"), "prompt": k.get("prompt") or "",
                                "depends_on": [str(d) for d in (k.get("depends_on") or [])][:12],
                                "selects": [str(d) for d in (k.get("selects") or [])][:12],
                                "selected_by": selected_by,
                                "implied_by": implied_by,
                                "defined_for_arch": k.get("defined_for_arch"),
                                "help": help_text.split("\n\n")[0][:600], "more": "\n\n" in help_text or len(help_text) > 600})
        elif out["found"]:
            from core.encoding import safe_split_lines
            path, line_s = detail["file_path"], int(detail.get("line_s") or detail.get("line") or 1)
            data, is_dir, _ = git_reader.read_file(sha, path)
            lines = safe_split_lines(data.decode("utf-8", errors="replace")) if data and not is_dir else []
            out.update(kind=detail.get("type_name"), file=path, line=line_s,
                       definitions=len(detail.get("definitions") or []) or 1,
                       signature=extract_signature(lines, line_s - 1) if lines else "",
                       doc=extract_doc(lines, line_s - 1) if lines else None)
            # Struct field offsets and cacheline positions from m_ast_attribute
            attrs = detail.get("attributes") or detail.get("definition", {}).get("attributes") or []
            attr_dict = {a.get("name"): a.get("val") for a in attrs if isinstance(a, dict)}
            ast_id = detail.get("ast_id") or detail.get("definition", {}).get("ast_id")
            if not attr_dict and ast_id:
                try:
                    from webapp.backend.database.pool import get_db_cursor
                    from webapp.backend.database.helpers import safe_decode
                    with get_db_cursor() as cursor:
                        cursor.execute("SELECT name, val FROM m_ast_attribute WHERE ast_id = %s;", (ast_id,))
                        attr_dict = {safe_decode(r.get("name")): safe_decode(r.get("val")) for r in cursor.fetchall()}
                except Exception:
                    pass
            if "offset_bits" in attr_dict or "offset_bytes" in attr_dict:
                try:
                    raw_offset_bits = int(attr_dict["offset_bits"]) if "offset_bits" in attr_dict else int(attr_dict["offset_bytes"]) * 8
                    offset_bytes = raw_offset_bits // 8
                    bit_offset = raw_offset_bits % 8
                    cacheline = offset_bytes // 64
                    cacheline_offset = offset_bytes % 64
                    out["field_offset"] = offset_bytes
                    out["bit_offset"] = bit_offset
                    out["cacheline"] = cacheline
                    out["cacheline_offset"] = cacheline_offset
                    if "size_bytes" in attr_dict:
                        out["size_bytes"] = int(attr_dict["size_bytes"])
                except (ValueError, TypeError):
                    pass
        self._cache[key] = out
        return out


hover_service = HoverService()
