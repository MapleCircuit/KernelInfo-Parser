# C AST Subsystem API Specification

The C AST subsystem parses C (`.c`, `.h`) and Assembly (`.S`, `.h`) translation units into relational database operations staged in a `ChangeSet` (`CS`).

## Authoritative Documentation
For exhaustive architectural specifications, AST node hierarchies, zone mechanics, and database staging patterns, refer to:
- [`CAST_API.md`](parser/c_ast/CAST_API.md)

## Core Entry Points & Components

| Component | File | Primary Responsibility |
| :--- | :--- | :--- |
| `c_ast_parse(CS)` | [`c_ast.py`](parser/c_ast/c_ast.py) | Main entrypoint; dispatches parsing based on git diff operation (`A`, `M`, `R`, `D`, `R100`). |
| `get_prior_tags(CS)` | [`c_ast.py`](parser/c_ast/c_ast.py) | Queries previous version active tags to enable cross-version tag recycling. |
| `close_prior_tags(CS)` | [`c_ast.py`](parser/c_ast/c_ast.py) | Emits closing updates for obsolete/removed tags. |
| `TokenList` | [`c_ast.py`](parser/c_ast/c_ast.py) | Fast ctypes tokenization, cursor annotation, coordinate extraction, and token streaming. |
| `Ast_Manager` | [`c_ast.py`](parser/c_ast/c_ast.py) | Drives libclang translation unit creation with kernel compilation arguments. |
| `Zone` / `Zone_Type` | [`c_ast_type.py`](parser/c_ast/c_ast_type.py) | Manages spatial code scopes (`Function_Args`, `Declared_Args`, `Compound_Stmt`, `Initializer_Expr`). |
| `C_Type` | [`c_ast_type.py`](parser/c_ast/c_ast_type.py) | Parses type specifiers, qualifiers (`CQual`), pointers, arrays, and multi-declarators. |
| `Line` | [`c_ast_type.py`](parser/c_ast/c_ast_type.py) | Spatial coordinate carrier (`line_pos`, `char_pos`, `code`). |
| `Ast` | [`c_ast_type.py`](parser/c_ast/c_ast_type.py) | Base intermediate node providing `within_range`, `exec_filter`, `extract_1arg`, `tag`, and `map_ast`. |

