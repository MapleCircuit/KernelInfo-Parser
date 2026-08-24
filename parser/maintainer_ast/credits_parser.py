"""parser/maintainer_ast/credits_parser.py - Parser for Linux CREDITS files.

Extracts contributor records (Name, Email, Web, PGP, Description, Snail-Mail)
from the kernel CREDITS file.
"""
from __future__ import annotations
import re
import logging
from typing import Sequence

from parser.maintainer_ast.maintainer_types import CreditsEntry

logger = logging.getLogger(__name__)

# Regex matching CREDITS tag lines like "N:\tLinus Torvalds"
CREDITS_TAG_RE = re.compile(r"^([A-Z]):\s*(.*)$")


class CreditsParser:
    """Parser for the Linux kernel CREDITS file."""

    def __init__(self, content: str) -> None:
        self.raw_content = content
        self.lines = content.splitlines()

    def parse(self) -> list[CreditsEntry]:
        """Parse raw CREDITS content into a list of structured CreditsEntry objects."""
        preamble_end = self._find_preamble_end()
        entries: list[CreditsEntry] = []

        current_fields: dict[str, list[str]] = {}
        entry_start_line = preamble_end + 1

        line_num = preamble_end
        while line_num < len(self.lines):
            raw_line = self.lines[line_num]
            line_idx = line_num + 1
            line = raw_line.strip()

            if not line:
                # Blank line marks end of entry
                if current_fields and "N" in current_fields:
                    entry = self._build_entry(current_fields, entry_start_line, line_idx - 1)
                    if entry:
                        entries.append(entry)
                    current_fields = {}
                    entry_start_line = line_idx + 1
                line_num += 1
                continue

            if re.match(r"^-{3,}$", line):
                # Separator line
                if current_fields and "N" in current_fields:
                    entry = self._build_entry(current_fields, entry_start_line, line_idx - 1)
                    if entry:
                        entries.append(entry)
                    current_fields = {}
                entry_start_line = line_idx + 1
                line_num += 1
                continue

            tag_match = CREDITS_TAG_RE.match(line)
            if tag_match:
                tag_char = tag_match.group(1)
                tag_val = tag_match.group(2).strip()

                if tag_char == "N" and current_fields and "N" in current_fields:
                    # New contributor starting without blank line separator
                    entry = self._build_entry(current_fields, entry_start_line, line_idx - 1)
                    if entry:
                        entries.append(entry)
                    current_fields = {}
                    entry_start_line = line_idx

                if not current_fields:
                    entry_start_line = line_idx

                if tag_char not in current_fields:
                    current_fields[tag_char] = []
                current_fields[tag_char].append(tag_val)
            else:
                # Non-tag continuation line (append to previous field if D or S)
                if current_fields:
                    if "D" in current_fields and "S" not in current_fields:
                        current_fields["D"].append(line)
                    elif "S" in current_fields:
                        current_fields["S"].append(line)

            line_num += 1

        # Flush final entry
        if current_fields and "N" in current_fields:
            entry = self._build_entry(current_fields, entry_start_line, len(self.lines))
            if entry:
                entries.append(entry)

        return entries

    def _find_preamble_end(self) -> int:
        """Find the line index where contributor entries begin, skipping the header."""
        for i, line in enumerate(self.lines):
            # Look for delimiter
            if re.match(r"^-{3,}$", line.strip()):
                return i + 1
            if line.strip().startswith("N:"):
                return i
        return 0

    def _build_entry(
        self,
        fields: dict[str, list[str]],
        line_s: int,
        line_e: int,
    ) -> CreditsEntry | None:
        """Construct a CreditsEntry instance from accumulated field dictionary."""
        name_parts = fields.get("N", [])
        name = " ".join(name_parts).strip()
        if not name:
            return None

        email = ", ".join(fields.get("E", [])).strip()
        web_page = ", ".join(fields.get("W", [])).strip()
        pgp_key = ", ".join(fields.get("P", [])).strip()
        description = " ".join(fields.get("D", [])).strip()
        snail_mail = "\n".join(fields.get("S", [])).strip()

        start_idx = max(0, line_s - 1)
        end_idx = min(len(self.lines), line_e)
        raw_text = "\n".join(self.lines[start_idx:end_idx])

        return CreditsEntry(
            name=name,
            email=email,
            web_page=web_page,
            pgp_key=pgp_key,
            description=description,
            snail_mail=snail_mail,
            line_s=line_s,
            line_e=line_e,
            raw_text=raw_text,
        )
