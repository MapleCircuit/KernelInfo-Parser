"""parser/maintainer_ast/maintainer_types.py - Data Models & Enums for Maintainer Subsystem."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import IntEnum


class MaintainerRole(IntEnum):
    """Role classification for subsystem members."""
    MAINTAINER = 1  # 'M'
    REVIEWER = 2    # 'R'
    PERSON = 3      # 'P' (legacy)
    OTHER = 4       # Other contributors / credit


class PatternType(IntEnum):
    """Category of file matching and exclusion pattern rules."""
    FILE = 1     # 'F' - File / directory pattern
    EXCLUDE = 2  # 'X' - File / directory exclusion pattern
    KEYWORD = 3  # 'K' - Content regex pattern
    REGEX = 4    # 'N' - File regex pattern


@dataclass
class MaintainerPerson:
    """Represents a maintainer, reviewer, or contributor."""
    name: str
    email: str
    role: MaintainerRole = MaintainerRole.MAINTAINER

    def __post_init__(self) -> None:
        self.name = self.name.strip().strip('"\'')
        self.email = self.email.strip().strip("<>\"' \t\r\n")


@dataclass
class PatternRule:
    """Represents a pattern rule within a maintainer section."""
    pat_type: PatternType
    pattern: str
    priority: int = 0

    def __post_init__(self) -> None:
        self.pattern = self.pattern.strip()


@dataclass
class MaintainerSection:
    """Represents an entire subsystem entry in MAINTAINERS."""
    name: str
    status: str = "Maintained"
    scm_tree: str = ""
    web_page: str = ""
    mailing_list: str = ""
    patchwork: str = ""
    members: list[MaintainerPerson] = field(default_factory=list)
    patterns: list[PatternRule] = field(default_factory=list)
    line_s: int = 1
    line_e: int = 1
    raw_text: str = ""

    def get_maintainers(self) -> list[MaintainerPerson]:
        """Return members designated as maintainers."""
        return [m for m in self.members if m.role == MaintainerRole.MAINTAINER]

    def get_reviewers(self) -> list[MaintainerPerson]:
        """Return members designated as reviewers."""
        return [m for m in self.members if m.role == MaintainerRole.REVIEWER]

    def get_file_patterns(self) -> list[str]:
        """Return list of 'F:' file inclusion pattern strings."""
        return [p.pattern for p in self.patterns if p.pat_type == PatternType.FILE]

    def get_exclude_patterns(self) -> list[str]:
        """Return list of 'X:' file exclusion pattern strings."""
        return [p.pattern for p in self.patterns if p.pat_type == PatternType.EXCLUDE]


@dataclass
class CreditsEntry:
    """Represents a contributor record from CREDITS."""
    name: str
    email: str = ""
    web_page: str = ""
    pgp_key: str = ""
    description: str = ""
    snail_mail: str = ""
    line_s: int = 1
    line_e: int = 1
    raw_text: str = ""

    def __post_init__(self) -> None:
        self.name = self.name.strip().strip('"\'')
        self.email = self.email.strip().strip("<>\"' \t\r\n")
        self.web_page = self.web_page.strip()
        self.pgp_key = self.pgp_key.strip()
        self.description = self.description.strip()
        self.snail_mail = self.snail_mail.strip()

