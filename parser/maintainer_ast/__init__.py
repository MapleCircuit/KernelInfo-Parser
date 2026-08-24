"""parser/maintainer_ast - Linux Kernel MAINTAINERS Parser Subsystem."""
from parser.maintainer_ast.maintainer_types import (
    MaintainerRole,
    PatternType,
    MaintainerPerson,
    PatternRule,
    MaintainerSection,
    CreditsEntry,
)
from parser.maintainer_ast.maintainer_parser import MaintainerParser, parse_person_string
from parser.maintainer_ast.credits_parser import CreditsParser
from parser.maintainer_ast.maintainer_matcher import MaintainerMatcher
from parser.maintainer_ast.maintainer_ast import (
    maintainer_ast_parse,
    MaintainerManager,
    credits_ast_parse,
    CreditsManager,
    query_maintainers_for_file,
    query_credits,
)

__all__ = [
    "MaintainerRole",
    "PatternType",
    "MaintainerPerson",
    "PatternRule",
    "MaintainerSection",
    "CreditsEntry",
    "MaintainerParser",
    "CreditsParser",
    "parse_person_string",
    "MaintainerMatcher",
    "maintainer_ast_parse",
    "MaintainerManager",
    "credits_ast_parse",
    "CreditsManager",
    "query_maintainers_for_file",
    "query_credits",
]

