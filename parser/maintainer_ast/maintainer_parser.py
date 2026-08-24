"""parser/maintainer_ast/maintainer_parser.py - Parser for Linux MAINTAINERS files.

Extracts subsystem sections, maintainers, reviewers, mailing lists, status, SCM trees,
and file matching/exclusion patterns from the kernel MAINTAINERS file.
"""
from __future__ import annotations
import re
import logging
from typing import Sequence

from parser.maintainer_ast.maintainer_types import (
    MaintainerRole,
    PatternType,
    MaintainerPerson,
    PatternRule,
    MaintainerSection,
)

logger = logging.getLogger(__name__)

# Regex matching tag lines like "M:\tLinus Torvalds <torvalds@linux-foundation.org>"
TAG_LINE_RE = re.compile(r"^([A-Za-z]):\s*(.*)$")

# Regex extracting "Name <email@domain>" or "<email@domain>" or "email@domain"
PERSON_RE = re.compile(r'^(?:["\']?(.*?)["\']?\s*)?<([^\s<>@]+@[^\s<>@]+)>$|^([^\s<>@]+@[^\s<>@]+)$')


def parse_person_string(text: str, role: MaintainerRole = MaintainerRole.MAINTAINER) -> list[MaintainerPerson]:
    """Parse one or multiple person/email definitions from a tag line."""
    text = text.strip()
    if not text:
        return []

    persons: list[MaintainerPerson] = []
    # If comma-separated multiple entries with brackets, split by commas outside brackets
    entries = []
    current: list[str] = []
    in_bracket = False
    for ch in text:
        if ch == "<":
            in_bracket = True
        elif ch == ">":
            in_bracket = False
        elif ch == "," and not in_bracket:
            part = "".join(current).strip()
            if part:
                entries.append(part)
            current = []
            continue
        current.append(ch)
    last_part = "".join(current).strip()
    if last_part:
        entries.append(last_part)

    for entry in entries:
        m = PERSON_RE.match(entry)
        if m:
            if m.group(2):
                name = m.group(1) or ""
                email = m.group(2)
            else:
                name = ""
                email = m.group(3) or ""
            persons.append(MaintainerPerson(name=name.strip(), email=email.strip(), role=role))
        else:
            # Fallback heuristic: find last <...>
            if "<" in entry and ">" in entry:
                s_idx = entry.find("<")
                e_idx = entry.find(">", s_idx)
                name = entry[:s_idx].strip().strip('"\'')
                email = entry[s_idx + 1 : e_idx].strip()
                persons.append(MaintainerPerson(name=name, email=email, role=role))
            else:
                # Raw text without brackets
                if "@" in entry:
                    persons.append(MaintainerPerson(name="", email=entry.strip(), role=role))
                else:
                    persons.append(MaintainerPerson(name=entry.strip(), email="", role=role))

    return persons


class MaintainerParser:
    """Parser for the Linux kernel MAINTAINERS file."""

    def __init__(self, content: str) -> None:
        self.raw_content = content
        self.lines = content.splitlines()

    def parse(self) -> list[MaintainerSection]:
        """Parse raw MAINTAINERS content into structured MaintainerSection objects."""
        preamble_end = self._find_preamble_end()
        sections: list[MaintainerSection] = []

        current_title_lines: list[str] = []
        current_tags: list[tuple[str, str, int]] = []
        section_start_line = preamble_end + 1

        line_num = preamble_end
        while line_num < len(self.lines):
            raw_line = self.lines[line_num]
            line_idx = line_num + 1
            line = raw_line.strip()

            if not line:
                # Empty line marks potential boundary of section
                if current_title_lines and current_tags:
                    sec = self._build_section(current_title_lines, current_tags, section_start_line, line_idx - 1)
                    if sec:
                        sections.append(sec)
                    current_title_lines = []
                    current_tags = []
                    section_start_line = line_idx + 1
                line_num += 1
                continue

            # Check if line is a separator line (e.g. "-----------------------------------")
            if re.match(r"^-{3,}$", line):
                if current_title_lines and current_tags:
                    sec = self._build_section(current_title_lines, current_tags, section_start_line, line_idx - 1)
                    if sec:
                        sections.append(sec)
                    current_title_lines = []
                    current_tags = []
                section_start_line = line_idx + 1
                line_num += 1
                continue

            tag_match = TAG_LINE_RE.match(line)
            if tag_match and len(tag_match.group(1)) == 1 and tag_match.group(1).isupper():
                tag_char = tag_match.group(1)
                tag_val = tag_match.group(2).strip()
                current_tags.append((tag_char, tag_val, line_idx))
            else:
                # Non-tag line. If we already have tags in current section, this is the start of a NEW section!
                if current_tags:
                    sec = self._build_section(current_title_lines, current_tags, section_start_line, line_idx - 1)
                    if sec:
                        sections.append(sec)
                    current_title_lines = [line]
                    current_tags = []
                    section_start_line = line_idx
                else:
                    if not current_title_lines:
                        section_start_line = line_idx
                    current_title_lines.append(line)

            line_num += 1

        # Final section flush
        if current_title_lines and current_tags:
            sec = self._build_section(current_title_lines, current_tags, section_start_line, len(self.lines))
            if sec:
                sections.append(sec)

        return sections

    def _find_preamble_end(self) -> int:
        """Find the index where section entries begin, skipping the header documentation."""
        for i, line in enumerate(self.lines):
            # Look for standard Maintainers List marker or first section header
            if "Maintainers List" in line or "MAINTAINERS LIST" in line:
                # Look for separator after this
                for j in range(i + 1, min(i + 20, len(self.lines))):
                    if re.match(r"^-{3,}$", self.lines[j].strip()):
                        return j + 1
                return i + 1

        # Fallback: scan for first sequence of Title followed immediately by tag lines
        for i in range(len(self.lines) - 2):
            if self.lines[i].strip() and not TAG_LINE_RE.match(self.lines[i].strip()):
                # Check if next non-empty line has a tag (M:, F:, etc.)
                for k in range(i + 1, min(i + 5, len(self.lines))):
                    nxt = self.lines[k].strip()
                    if not nxt:
                        continue
                    if TAG_LINE_RE.match(nxt):
                        # Ensure this isn't the descriptions section (e.g. "P: Person (obsolete)")
                        if "Descriptions of section entries" not in self.lines[i]:
                            return i
                    break

        return 0

    def _build_section(
        self,
        title_lines: Sequence[str],
        tags: Sequence[tuple[str, str, int]],
        line_s: int,
        line_e: int,
    ) -> MaintainerSection | None:
        """Construct a MaintainerSection instance from parsed title lines and tag tuples."""
        name = " ".join(t.strip() for t in title_lines if t.strip())
        if not name or not tags:
            return None

        # Clean section title (remove extra spaces)
        name = re.sub(r"\s+", " ", name).strip()

        status = "Maintained"
        scm_tree = ""
        web_page = ""
        mailing_list = ""
        patchwork = ""
        members: list[MaintainerPerson] = []
        patterns: list[PatternRule] = []

        member_priority = 0
        pattern_priority = 0

        for tag_char, tag_val, _line_num in tags:
            if tag_char == "M":
                parsed_persons = parse_person_string(tag_val, role=MaintainerRole.MAINTAINER)
                members.extend(parsed_persons)
            elif tag_char == "R":
                parsed_persons = parse_person_string(tag_val, role=MaintainerRole.REVIEWER)
                members.extend(parsed_persons)
            elif tag_char == "P":
                parsed_persons = parse_person_string(tag_val, role=MaintainerRole.PERSON)
                members.extend(parsed_persons)
            elif tag_char == "L":
                # Clean mailing list comments like "(subscribers-only)" or "(open list)"
                ml = tag_val
                if not mailing_list:
                    mailing_list = ml
                else:
                    mailing_list += f", {ml}"
            elif tag_char == "S":
                status = tag_val
            elif tag_char == "W":
                if not web_page:
                    web_page = tag_val
                else:
                    web_page += f", {tag_val}"
            elif tag_char == "T":
                if not scm_tree:
                    scm_tree = tag_val
                else:
                    scm_tree += f", {tag_val}"
            elif tag_char == "Q" or tag_char == "B" or tag_char == "C":
                if not patchwork:
                    patchwork = tag_val
                else:
                    patchwork += f", {tag_val}"
            elif tag_char == "F":
                patterns.append(PatternRule(pat_type=PatternType.FILE, pattern=tag_val, priority=pattern_priority))
                pattern_priority += 1
            elif tag_char == "X":
                patterns.append(PatternRule(pat_type=PatternType.EXCLUDE, pattern=tag_val, priority=pattern_priority))
                pattern_priority += 1
            elif tag_char == "K":
                patterns.append(PatternRule(pat_type=PatternType.KEYWORD, pattern=tag_val, priority=pattern_priority))
                pattern_priority += 1
            elif tag_char == "N":
                patterns.append(PatternRule(pat_type=PatternType.REGEX, pattern=tag_val, priority=pattern_priority))
                pattern_priority += 1

        # Extract raw text slice
        start_idx = max(0, line_s - 1)
        end_idx = min(len(self.lines), line_e)
        raw_text = "\n".join(self.lines[start_idx:end_idx])

        return MaintainerSection(
            name=name,
            status=status,
            scm_tree=scm_tree,
            web_page=web_page,
            mailing_list=mailing_list,
            patchwork=patchwork,
            members=members,
            patterns=patterns,
            line_s=line_s,
            line_e=line_e,
            raw_text=raw_text,
        )
