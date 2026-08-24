"""parser/maintainer_ast/maintainer_matcher.py - File-to-Maintainer Pattern Matching Engine.

Implements Linux kernel scripts/get_maintainer.pl pattern matching rules:
- Trailing slash (e.g. 'drivers/net/'): recursive match for directory and all descendants.
- Direct wildcard (e.g. 'drivers/net/*'): match files directly under directory.
- Generic glob (e.g. '*/net/*', 'drivers/net/3c505*'): wildcard path matching.
- Exact path (e.g. 'include/linux/sched.h'): exact file matching.
- Exclusions ('X:'): overrides 'F:' matches for specific paths/subdirectories.
"""
from __future__ import annotations
import fnmatch
import re
from typing import Sequence
from collections import defaultdict

from parser.maintainer_ast.maintainer_types import (
    PatternType,
    MaintainerSection,
)


def _pattern_to_regex(pattern: str) -> re.Pattern:
    """Convert a MAINTAINERS file/exclude pattern to a compiled regex."""
    pattern = pattern.strip()
    if pattern.endswith("/"):
        # Trailing slash means recursive match under directory
        prefix = re.escape(pattern)
        return re.compile(f"^{prefix}.*")

    # If pattern contains wildcards
    if "*" in pattern or "?" in pattern or "[" in pattern:
        regex_str = fnmatch.translate(pattern)
        return re.compile(regex_str)

    # Exact match or directory prefix match
    escaped = re.escape(pattern)
    return re.compile(f"^{escaped}(?:/.*)?$")


class CompiledSectionRules:
    """Holds compiled include ('F:') and exclude ('X:') matchers for a section."""

    def __init__(self, section: MaintainerSection) -> None:
        self.section = section
        self.exact_includes: set[str] = set()
        self.prefix_includes: list[str] = []
        self.regex_includes: list[re.Pattern] = []

        self.exact_excludes: set[str] = set()
        self.prefix_excludes: list[str] = []
        self.regex_excludes: list[re.Pattern] = []

        for p in section.patterns:
            pat = p.pattern.strip()
            if not pat:
                continue

            if p.pat_type == PatternType.FILE:
                if pat.endswith("/"):
                    self.prefix_includes.append(pat)
                elif "*" in pat or "?" in pat or "[" in pat:
                    self.regex_includes.append(_pattern_to_regex(pat))
                else:
                    self.exact_includes.add(pat)
                    self.prefix_includes.append(f"{pat}/")

            elif p.pat_type == PatternType.EXCLUDE:
                if pat.endswith("/"):
                    self.prefix_excludes.append(pat)
                elif "*" in pat or "?" in pat or "[" in pat:
                    self.regex_excludes.append(_pattern_to_regex(pat))
                else:
                    self.exact_excludes.add(pat)
                    self.prefix_excludes.append(f"{pat}/")

    def matches(self, file_path: str) -> bool:
        """Check if file_path matches this section's F: patterns without matching any X: patterns."""
        # 1. Check exclusions first
        if self._is_excluded(file_path):
            return False

        # 2. Check inclusions
        if file_path in self.exact_includes:
            return True

        for pfx in self.prefix_includes:
            if file_path.startswith(pfx):
                return True

        for regex in self.regex_includes:
            if regex.match(file_path):
                return True

        return False

    def _is_excluded(self, file_path: str) -> bool:
        if file_path in self.exact_excludes:
            return True

        for pfx in self.prefix_excludes:
            if file_path.startswith(pfx):
                return True

        for regex in self.regex_excludes:
            if regex.match(file_path):
                return True

        return False


class MaintainerMatcher:
    """Indexed pattern matcher across all maintainer sections."""

    def __init__(self, sections: Sequence[MaintainerSection]) -> None:
        self.sections = list(sections)
        self.compiled_rules: list[CompiledSectionRules] = [CompiledSectionRules(sec) for sec in self.sections]

        # Fast lookup indices by exact paths and top-level directory prefixes
        self.exact_index: dict[str, list[CompiledSectionRules]] = defaultdict(list)
        self.prefix_index: list[tuple[str, CompiledSectionRules]] = []
        self.general_rules: list[CompiledSectionRules] = []

        for rule in self.compiled_rules:
            has_specific = False
            for exact_path in rule.exact_includes:
                self.exact_index[exact_path].append(rule)
                has_specific = True
            for pfx in rule.prefix_includes:
                self.prefix_index.append((pfx, rule))
                has_specific = True
            if rule.regex_includes or not has_specific:
                self.general_rules.append(rule)

    def match_file(self, file_path: str) -> list[MaintainerSection]:
        """Find all maintainer sections responsible for the given file path."""
        file_path = file_path.strip().lstrip("/")
        matching_sections: list[MaintainerSection] = []
        seen_secs: set[str] = set()

        for rule in self.compiled_rules:
            if rule.matches(file_path):
                if rule.section.name not in seen_secs:
                    seen_secs.add(rule.section.name)
                    matching_sections.append(rule.section)

        return matching_sections

    def match_all_files(self, file_paths: Sequence[str]) -> dict[str, list[MaintainerSection]]:
        """Batch match a collection of file paths against all sections."""
        results: dict[str, list[MaintainerSection]] = {}
        for path in file_paths:
            cleaned = path.strip().lstrip("/")
            results[cleaned] = self.match_file(cleaned)
        return results
