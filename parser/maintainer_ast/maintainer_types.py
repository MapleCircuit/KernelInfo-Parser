"""parser/maintainer_ast/maintainer_types.py - Data Models & Enums for Maintainer Subsystem."""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from enum import IntEnum

# C0 controls and DEL, which `str.strip()` leaves in place when they sit inside the text. Tab,
# newline and carriage return are left to the `\s+` collapse below. The C1 range (\x80-\x9f) is
# deliberately NOT included: in this corpus those bytes are the tail of a UTF-8 sequence decoded as
# latin-1 ("Michał" arriving as "MichaÅ\x82"), so treating them as controls would mangle the name
# further and destroy what a later mojibake repair needs.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_identity_text(text: str | None) -> str:
    """
    A contributor's name or e-mail as it is stored and deduplicated by.

    Control characters become spaces and runs of whitespace collapse to one, because the kernel
    sources carry stray bytes and doubled spaces that `.strip()` keeps: commit `a929478f` signs off
    as `Olof Johansson\\x01 <olof@lixom.net\\x01>`, and `Ruslan  Sushko` appears beside
    `Ruslan Sushko`. Neither is visible, so each silently forks one contributor into a second
    `m_maintainer_person` identity, and a control byte also makes the name sort in MySQL's
    collation differently from the same string in Python. No-break space (and the rest of Unicode
    whitespace) collapses too, since `Vitor\\xa0Soares` is the same person as `Vitor Soares`.
    """
    if not text:
        return ""
    return re.sub(r"\s+", " ", _CONTROL_CHARS_RE.sub(" ", text)).strip()


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


@dataclass(slots=True)
class MaintainerPerson:
    """Represents a maintainer, reviewer, or contributor."""
    name: str
    email: str
    role: MaintainerRole = MaintainerRole.MAINTAINER
    person_id: int | None = None

    def __post_init__(self) -> None:
        self.name = clean_identity_text(self.name).strip('"\'').strip()
        self.email = clean_identity_text(self.email).strip("<>\"'").strip()


@dataclass(slots=True)
class PatternRule:
    """Represents a pattern rule within a maintainer section."""
    pat_type: PatternType
    pattern: str
    priority: int = 0

    def __post_init__(self) -> None:
        self.pattern = self.pattern.strip()


@dataclass(slots=True)
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
    sec_id: int | None = None

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


@dataclass(slots=True)
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
    credit_id: int | None = None
    person_id: int | None = None

    def __post_init__(self) -> None:
        self.name = clean_identity_text(self.name).strip('"\'').strip()
        self.email = clean_identity_text(self.email).strip("<>\"'").strip()
        self.web_page = self.web_page.strip()
        self.pgp_key = self.pgp_key.strip()
        self.description = self.description.strip()
        self.snail_mail = self.snail_mail.strip()

