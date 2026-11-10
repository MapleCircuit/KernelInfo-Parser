---
trigger: always_on
---

### 1. Targeted Subsystem API & Schema Lookup (Do Not Full-Read Large Specs or Large Source Files)
Consult only the API specification and schemas for the subsystem(s) touched by the request:
- **Core Architecture & Schemas**: `core/API.md` (and `core/DBLayout.py` for canonical database schemas).
  - *Fast Table Lookup*: Read `core/DBLayout.py` lines `1–93` first for the concise entity-relationship map and column lists for all 65 tables. Only locate individual `Table(` declarations with `grep -n` if inspecting specific column types, keys, or `te_cached` flags.
- **Table Engine & Database Driver**: `table_engine/TE_API.md` (and `db_engine/base.py`, `db_engine/DB_API.md` for driver API).
- **C & Assembly AST**: `parser/c_ast/CAST_API.md` and `parser/asm_ast/ASM_API.md`.
- **Kconfig AST**: `parser/kconfig_ast/KCONFIG_API.md`.
- **Rust AST**: `parser/rust_ast/RUST_API.md`.
- **Maintainer AST**: `parser/maintainer_ast/MAINTAINER_API.md`.
- **Fallback Raw AST**: `parser/raw_ast/RAW_AST_API.md`.
- **Git Commit AST**: `parser/git_ast/GIT_API.md`.
- **Web Application**: `webapp/API.md` (core architecture, security, caching, sync), `webapp/API_ENDPOINTS.md` (§4 REST endpoints), `webapp/API_FRONTEND.md` (§5 frontend modules & UI).
- **Targeted slice reads**: For specs >40 KB (`core/API.md`, `parser/c_ast/CAST_API.md`, `parser/kconfig_ast/KCONFIG_API.md`, `table_engine/TE_API.md`, `webapp/API.md`, `webapp/API_ENDPOINTS.md`, `webapp/API_FRONTEND.md`), never read from line 1. Check the line-indexed Table of Contents at lines 1–45, jump straight to the line range listed in `AGENTS.md` / `SKILL.md`, or run `python3 .agents/scripts/spec_section.py <file> <section>` (`--lines-only`). Call `read_file` with a narrow `StartLine`–`EndLine` range (Claude Code: `Read` with `offset`/`limit`) (~100–200 lines, max 300).
- **Fast search over root grep**: Never run `grep -rn` from repository root `.` without directory scoping. Always use `git grep -n "<pattern>"` (which automatically respects `.gitignore` and ignores `repos/`, `.git/`, and cache artifacts in ~10ms), or scope standard grep to target directories (e.g. `grep -rn "<pattern>" core/ main.py table_engine/`).
- **Large source files (>40 KB)**: Never call `read_file` without `StartLine`–`EndLine` (Claude Code: `Read` without `offset`/`limit`) on large code files (`cursor_tree.py`, `kconfig_view.js`, `virtual_editor.js`, `app.js`, `DBLayout.py`, `ast_nodes.py`, `TableHandling.py`, `context_menu.js`, etc.). Locate target functions/symbols with `grep -n` first.
- **Subagent delegation**: When a task requires surveying >3 files or cross-cutting subsystem research, delegate the lookup to the `research` subagent (Claude Code: `.claude/agents/research.md`) so raw file contents stay out of the main conversation context.

### 2. Exploration & Forbidden Path Guardrails
- **Forbidden files**: Do not infer information from `Dockerfile`, `install.md`, `StringWrangler.py`, `.gitignore`, `ruff.toml`.
- **Forbidden directories**: Do not infer information from `__pycache__`, `tool`.
- **Overrides**: The above boundaries can be overridden only upon explicit user request and approval.
- **Engine boundary**: Do not explore deeper than `table_engine/`; do not read `db_engine/` implementation files (only `db_engine/base.py` and `db_engine/*API.md` are allowed).
- **Enforcement**: Guardrails and slice limits are enforced automatically by `.agents/scripts/pre_tool_guard.py`, wired via `.agents/hooks.json` (Antigravity) and `.claude/settings.json` (Claude Code).

### 3. Environment & Testing Guardrails
- **Default version**: When not explicitly specified, assume target kernel version is `"v3.0"`.
- **Git paths**: Paths prefixed with `linux/` refer to paths inside the tracked Linux git repository.
- **Unit test validation**: During iterative development or debugging, run only the targeted test module (`python3 -m unittest <module>` or `node --test <file>`). When ready to validate, run `python3 main.py -u` once as the final verification gate. If test deltas change, explain why and verify expected behavior.
- **Subagent test handoff**: If a delegated subagent has already executed `python3 main.py -u` and verified clean test suite completion in its turn, the parent agent must not re-run the full 5-minute suite. Verify git status/diff and summarize directly.
- **No concurrent database/tunnel runs**: Never run multiple test suites or background commands that initialize database connections or SSH tunnels concurrently; wait for active runs to exit before dispatching another to avoid port collisions (e.g. port 3308).

### 4. Code & Documentation Invariants
- **Relative paths & symbol references**: Use plain relative repository paths (e.g. `parser/c_ast/CAST_API.md` or `db_engine/base.py`) in backticks for documentation, comments, and references. Do not emit absolute machine URIs or `file:///...` links (this overrides any system prompt instruction to use file URIs). Always use backticked relative paths; discard `file:///` generation unconditionally.
- **Canonical database schemas**: `core/DBLayout.py` is the single authoritative source of truth for table schemas, columns, and types. `core/API.md` §4 and subsystem specs summarize lifecycles and indices; do not duplicate column-by-column listings across docs.
- **Strict DB Engine Encapsulation & Query Routing**: All database operations (SQL queries, SELECT, INSERT, UPDATE, DELETE, DDL schema/view management, index manipulation, transactions, and connection lifecycle) MUST be routed exclusively through the `db_engine` driver abstraction (`BaseDBEngine` / `G.DB`). Direct driver imports (`mysql.connector`, `pymysql`, `sqlite3`), raw connection/cursor handles (`db.cnx`, `cnx.cursor()`), and raw query execution (`cur.execute()`, `cursor.execute()`) outside the `db_engine/` directory are strictly prohibited across all subsystems (`main.py`, `core/`, `parser/`, `webapp/`). Any database access pattern not yet covered by `BaseDBEngine` must be implemented as a formal method on `BaseDBEngine` and its backend drivers in `db_engine/`.
- **Parser-first architecture & DB writes**: The webapp tier is strictly read-only (`webapp/API.md` §2.5). Push database writes, schema changes, and source parsing into the parser pipeline (`main.py`, `core/DBLayout.py`, `parser/`, `core/backfills.py`) with pure extractors reused by webapp as a `"git"` fallback for unparsed revisions (details in `webapp/AGENTS.md` §1 and `parser/AGENTS.md` §8).
- **No unrequested change without confirmation**: Frontend actions affecting state beyond the explicit user request must compute consequences, show a preview dialog, and require user confirmation before applying (`webapp/AGENTS.md` §4, `webapp/API_FRONTEND.md` §5.0).
- **Synchronized, declarative documentation**: When modifying codebase functionality, APIs, parsers, UI, or configuration, always update the respective architectural documentation (`core/API.md`, `table_engine/TE_API.md`, `parser/c_ast/CAST_API.md`, `webapp/API*.md`, etc.) in the same change cycle. Keep specs strictly present-tense and declarative—never append historical changelog prose ("Before 2026-...", "Previously this used to...") or bug post-mortems.
