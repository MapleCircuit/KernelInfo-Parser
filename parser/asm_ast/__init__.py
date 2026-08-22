"""parser/asm_ast - Native Assembly AST Parsing Module.

Supports parsing of assembly header directives and standalone .S / .s assembly source files.
"""
from parser.asm_ast.asm_ast import asm_ast_parse

__all__ = ["asm_ast_parse"]
