"""parser/doc_parser.py - High-speed Documentation & Text Cross-File Reference Scanner.

Scans documentation files (Documentation/, *.txt, *.rst, README*) for references
to repository files, validating matches against active version files (m_file_name)
in O(1) time and extracting exact source lines and context snippets.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass
class DocReference:
    """Represents a cross-file reference discovered inside a documentation or text file."""
    source_fid: int
    target_fnid: int
    line_no: int
    details: str


class DocReferenceScanner:
    """Deterministic, high-speed scanner extracting validated repository file references from text."""

    # Matches relative paths with directories and extensions (e.g. fs/ext4/super.c, ext4.txt, Documentation/filesystems/ext4.txt)
    PATH_WITH_EXT_RE = re.compile(
        r'(?:\b|[a-zA-Z0-9_\-\.]+/)[a-zA-Z0-9_\-\.]+(?:/[a-zA-Z0-9_\-\.]+)*\.[a-zA-Z0-9_\-]+'
    )

    # Matches Sphinx kernel-doc directives (e.g. .. kernel-doc:: include/linux/fs.h)
    KERNEL_DOC_RE = re.compile(r"^\s*\.\.\s+kernel-doc::\s*(\S+)")

    # Matches known extensionless kernel paths (e.g. fs/ext4/Makefile, drivers/net/Kbuild, MAINTAINERS)
    PATH_SPECIAL_RE = re.compile(
        r'(?:[a-zA-Z0-9_\-\.]+/)*(?:Makefile|Kbuild|MAINTAINERS|CREDITS|Kconfig)\b'
    )

    # Matches Sphinx :doc:`path` or :doc:`title <path>` references (often extensionless)
    DOC_ROLE_RE = re.compile(r":doc:`(?:[^<`]*<)?([^>`]+)>?`")

    # Matches legacy DocBook directives (!E, !I, !F, !P, !C) in Documentation/DocBook/*.tmpl
    DOCBOOK_DIRECTIVE_RE = re.compile(r"^!([EIFPC])\s*([a-zA-Z0-9_\-\./]+)(?:\s+(.*))?$")

    # Matches Sphinx kernel-doc option lines: :functions: or :identifiers:
    KERNEL_DOC_SYMBOLS_RE = re.compile(r"^\s*:(?:functions|identifiers):\s*(.+)$")

    # URL and email prefix ignore patterns
    IGNORED_PREFIXES = ("http://", "https://", "ftp://", "git://", "mailto:", "www.")

    # Punctuation to strip from candidate path boundaries
    STRIP_CHARS = "\"'`<>()[];,: \t\r\n"

    @classmethod
    def is_doc_candidate(cls, path: str) -> bool:
        """Determine if a file path qualifies as a documentation/text candidate."""
        clean = path.replace("\\", "/").strip("/")
        if clean.startswith("Documentation/"):
            return True
        if clean.endswith((".txt", ".rst", ".tmpl")):
            return True
        base = os.path.basename(clean)
        if base.startswith("README") or base in ("MAINTAINERS", "CREDITS"):
            return True
        return False

    @classmethod
    def sanitize_candidate(cls, raw: str) -> str:
        """Sanitize candidate string by stripping surrounding delimiters, line numbers, and diff prefixes."""
        s = raw.strip()
        # Filter out URLs and emails immediately
        if any(s.startswith(p) for p in cls.IGNORED_PREFIXES) or "@" in s:
            return ""

        # Strip surrounding punctuation
        s = s.strip(cls.STRIP_CHARS)

        # Strip trailing line numbers (e.g. path/to/file.c:123 or :L123)
        s = re.sub(r":L?\d+$", "", s)

        # Strip git diff prefixes (a/ or b/)
        if s.startswith("a/") or s.startswith("b/"):
            s_diff = s[2:]
            return s_diff.strip(cls.STRIP_CHARS)

        return s.strip(cls.STRIP_CHARS)

    def scan_file_content(
        self,
        content: str,
        source_fid: int,
        fnid_by_path: dict[str, int],
        source_path: str = "",
    ) -> list[DocReference]:
        """Scan text content and extract all validated cross-file references.

        Args:
            content: Raw string content of the document.
            source_fid: File ID of the document containing the references.
            fnid_by_path: Fast O(1) lookup dictionary mapping repository relative path -> fnid.
            source_path: Optional relative path of source file (to filter self-references).

        Returns:
            List of validated DocReference instances.
        """
        references: list[DocReference] = []
        seen: set[tuple[int, int]] = set()  # (target_fnid, line_no) to prevent duplicate references per line
        source_dir = os.path.dirname(source_path) if source_path else ""

        lines = content.splitlines()
        n_lines = len(lines)
        idx = 0
        while idx < n_lines:
            line_no = idx + 1
            line = lines[idx]
            idx += 1
            line_str = line.strip()
            if not line_str or line_str.startswith("#!"):
                continue

            candidates: dict[str, str] = {}

            def _add_cand(c: str, ext: str = "") -> None:
                if not c:
                    return
                if c not in candidates or (ext and not candidates[c]):
                    candidates[c] = ext

            # Pre-filter fast boolean check for kernel-doc directive
            if "kernel-doc" in line_str:
                m_kdoc = self.KERNEL_DOC_RE.match(line)
                if m_kdoc:
                    cand = self.sanitize_candidate(m_kdoc.group(1))
                    if cand:
                        symbols_extra = ""
                        # Inspect subsequent indented lines for :functions: or :identifiers:
                        peek = idx
                        while peek < n_lines and (lines[peek].startswith(" ") or lines[peek].startswith("\t")):
                            sub = lines[peek].strip()
                            m_sym = self.KERNEL_DOC_SYMBOLS_RE.match(sub)
                            if m_sym:
                                symbols_extra = f" ({sub})"
                                break
                            peek += 1
                        _add_cand(cand, symbols_extra)

            # Pre-filter fast boolean check for legacy DocBook directives
            if line_str.startswith("!") and len(line_str) >= 2 and line_str[1] in "EIFPC":
                m_db = self.DOCBOOK_DIRECTIVE_RE.match(line_str)
                if m_db:
                    cand = self.sanitize_candidate(m_db.group(2))
                    extra_syms = m_db.group(3)
                    extra = f" [{extra_syms.strip()}]" if extra_syms else ""
                    _add_cand(cand, extra)

            # Pre-filter fast boolean check for Sphinx :doc: role
            if ":doc:" in line_str:
                for match in self.DOC_ROLE_RE.finditer(line_str):
                    _add_cand(self.sanitize_candidate(match.group(1)))

            # Pre-filter fast boolean check before PATH_SPECIAL_RE
            if any(s in line_str for s in ("Makefile", "Kbuild", "MAINTAINERS", "CREDITS", "Kconfig")):
                for match in self.PATH_SPECIAL_RE.finditer(line):
                    _add_cand(self.sanitize_candidate(match.group(0)))

            # Pre-filter fast boolean check before PATH_WITH_EXT_RE (must contain dot or slash)
            if "." in line_str or "/" in line_str:
                for match in self.PATH_WITH_EXT_RE.finditer(line):
                    _add_cand(self.sanitize_candidate(match.group(0)))

            for cand, extra in candidates.items():
                # Check direct path in active file map
                target_fnid = fnid_by_path.get(cand)
                resolved_cand = cand
                if target_fnid is None and cand.startswith("linux/"):
                    target_fnid = fnid_by_path.get(cand[6:])
                    resolved_cand = cand[6:]
                if target_fnid is None and source_dir:
                    rel_path = os.path.normpath(os.path.join(source_dir, cand)).replace("\\", "/")
                    if rel_path in fnid_by_path:
                        target_fnid = fnid_by_path[rel_path]
                        resolved_cand = rel_path

                # Extensionless :doc: fallback: try .rst and .txt extensions
                if target_fnid is None and not os.path.splitext(cand)[1]:
                    for ext in (".rst", ".txt"):
                        c_ext = cand + ext
                        if c_ext in fnid_by_path:
                            target_fnid = fnid_by_path[c_ext]
                            resolved_cand = c_ext
                            break
                        if source_dir:
                            rel_ext = os.path.normpath(os.path.join(source_dir, c_ext)).replace("\\", "/")
                            if rel_ext in fnid_by_path:
                                target_fnid = fnid_by_path[rel_ext]
                                resolved_cand = rel_ext
                                break

                if target_fnid is not None:
                    # Avoid self-references
                    if source_path and (resolved_cand == source_path or f"linux/{resolved_cand}" == source_path):
                        continue

                    key = (target_fnid, line_no)
                    if key not in seen:
                        seen.add(key)
                        snippet = f"{line_str}{extra}"[:160]
                        references.append(DocReference(
                            source_fid=source_fid,
                            target_fnid=target_fnid,
                            line_no=line_no,
                            details=snippet,
                        ))

        return references
