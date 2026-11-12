"""parser/man_ast - Linux man-pages roff parser, page facts, and repository path helpers (pure)."""
from parser.man_ast.man_facts import FAMILY, applies, decode, encode, extract
from parser.man_ast.man_parser import FORMAT, parse, plain_text, so_target
from parser.man_ast.man_paths import (
    SECTION_ORDER,
    candidate_paths,
    page_of_path,
    resolve_so,
    section_rank,
)

__all__ = [
    "FAMILY", "FORMAT", "SECTION_ORDER", "applies", "candidate_paths", "decode", "encode", "extract",
    "page_of_path", "parse", "plain_text", "resolve_so", "section_rank", "so_target",
]
