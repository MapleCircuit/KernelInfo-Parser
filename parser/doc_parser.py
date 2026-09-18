"""parser/doc_parser.py - High-speed Documentation & Text Cross-File Reference Scanner.

Scans documentation files (Documentation/, *.txt, *.rst, README*) for references
to repository files, validating matches against active version files (m_file_name)
in O(1) time and extracting exact source lines and context snippets.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class DocReference:
    """Represents a cross-file reference discovered inside a documentation or text file."""
    source_fid: int
    target_fnid: int
    line_no: int
    details: str


class DocReferenceScanner:
    """Deterministic, high-speed scanner extracting validated repository file references from text."""

    # Matches relative paths with directories and extensions (e.g. fs/ext4/super.c, Documentation/filesystems/ext4.txt)
    PATH_WITH_EXT_RE = re.compile(
        r'(?:\b|[a-zA-Z0-9_\-\.]+/)[a-zA-Z0-9_\-\.]+(?:/[a-zA-Z0-9_\-\.]+)+\.[a-zA-Z0-9_\-]+'
    )

    # Matches known extensionless kernel paths (e.g. fs/ext4/Makefile, drivers/net/Kbuild, MAINTAINERS)
    PATH_SPECIAL_RE = re.compile(
        r'(?:[a-zA-Z0-9_\-\.]+/)*(?:Makefile|Kbuild|MAINTAINERS|CREDITS|Kconfig)\b'
    )

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
        if clean.endswith((".txt", ".rst")):
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

        for line_no, line in enumerate(content.splitlines(), start=1):
            line_str = line.strip()
            if not line_str or line_str.startswith("#!"):
                continue

            candidates: set[str] = set()

            for match in self.PATH_WITH_EXT_RE.finditer(line):
                cand = self.sanitize_candidate(match.group(0))
                if cand:
                    candidates.add(cand)

            for match in self.PATH_SPECIAL_RE.finditer(line):
                cand = self.sanitize_candidate(match.group(0))
                if cand:
                    candidates.add(cand)

            for cand in candidates:
                # Check direct path in active file map
                target_fnid = fnid_by_path.get(cand)
                if target_fnid is None:
                    # Also try stripping leading linux/ if present in text
                    if cand.startswith("linux/"):
                        target_fnid = fnid_by_path.get(cand[6:])

                if target_fnid is not None:
                    # Avoid self-references
                    if source_path and (cand == source_path or cand == f"linux/{source_path}"):
                        continue

                    key = (target_fnid, line_no)
                    if key not in seen:
                        seen.add(key)
                        snippet = line_str[:160]
                        references.append(DocReference(
                            source_fid=source_fid,
                            target_fnid=target_fnid,
                            line_no=line_no,
                            details=snippet,
                        ))

        return references
