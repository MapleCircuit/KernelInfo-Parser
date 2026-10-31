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


_WILDCARD_CHARS = ("*", "?", "[")


def _literal_dir_prefix(pattern: str) -> str:
    """Directory part of a wildcard pattern's literal prefix ('drivers/net/3c505*' -> 'drivers/net/').

    Every path the pattern matches starts with it, because fnmatch patterns are anchored at the start.
    Returns '' when the pattern has a wildcard before its first '/'.
    """
    cut = min((i for i in (pattern.find(c) for c in _WILDCARD_CHARS) if i != -1), default=len(pattern))
    return pattern[: pattern.rfind("/", 0, cut) + 1]


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
        # Directory prefixes every regex include requires ('' when one can match anywhere)
        self.regex_include_dirs: set[str] = set()

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
                    self.regex_include_dirs.add(_literal_dir_prefix(pat))
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

        # Fast lookup indices by exact paths and exact directory prefixes
        self.exact_index: dict[str, list[CompiledSectionRules]] = defaultdict(list)
        self.dir_prefix_map: dict[str, list[CompiledSectionRules]] = defaultdict(list)
        # Sections with wildcard 'F:' patterns, in section order. Each is filed under the directory prefixes
        # its wildcards require, so a file only tests the ones filed under its ancestor directories (plus
        # those whose wildcards can match anywhere). Sections without any 'F:' pattern never match.
        self.general_rules: list[CompiledSectionRules] = []
        self._wildcard_dir_index: dict[str, list[int]] = defaultdict(list)
        self._wildcard_anywhere: list[int] = []
        self._cache: dict[str, list[MaintainerSection]] = {}

        for rule in self.compiled_rules:
            for exact_path in rule.exact_includes:
                self.exact_index[exact_path].append(rule)
            for pfx in rule.prefix_includes:
                self.dir_prefix_map[pfx].append(rule)
            if rule.regex_includes:
                pos = len(self.general_rules)
                self.general_rules.append(rule)
                if "" in rule.regex_include_dirs:
                    self._wildcard_anywhere.append(pos)
                else:
                    for dir_prefix in rule.regex_include_dirs:
                        self._wildcard_dir_index[dir_prefix].append(pos)

    def match_file(self, file_path: str) -> list[MaintainerSection]:
        """Find all maintainer sections responsible for the given file path using indexed lookups."""
        file_path = file_path.strip().lstrip("/")
        if not file_path:
            return []

        if file_path in self._cache:
            return self._cache[file_path]

        matching_sections: list[MaintainerSection] = []
        seen_secs: set[str] = set()

        # 1. Exact path matches
        for rule in self.exact_index.get(file_path, ()):
            if rule.section.name not in seen_secs and rule.matches(file_path):
                seen_secs.add(rule.section.name)
                matching_sections.append(rule.section)

        # 2. Hierarchical ancestor directory prefix matches
        wildcard_candidates = set(self._wildcard_anywhere)
        slash_idx = file_path.find("/")
        while slash_idx != -1:
            pfx = file_path[: slash_idx + 1]
            for rule in self.dir_prefix_map.get(pfx, ()):
                if rule.section.name not in seen_secs and rule.matches(file_path):
                    seen_secs.add(rule.section.name)
                    matching_sections.append(rule.section)
            wildcard_candidates.update(self._wildcard_dir_index.get(pfx, ()))
            slash_idx = file_path.find("/", slash_idx + 1)

        # 3. Wildcard rules that can match this path, in section order
        for pos in sorted(wildcard_candidates):
            rule = self.general_rules[pos]
            if rule.section.name not in seen_secs and rule.matches(file_path):
                seen_secs.add(rule.section.name)
                matching_sections.append(rule.section)

        self._cache[file_path] = matching_sections
        return matching_sections
