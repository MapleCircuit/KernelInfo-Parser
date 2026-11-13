---
name: parser-snapshot-update
description: >-
  Workflow for modifying an AST or file parser in parser/, running targeted parser
  unit tests, and verifying or regenerating JSON golden snapshots.
---

# Modify an AST Parser & Verify Snapshots

## 1. Targeted Spec Slice Lookup
Never read `*_API.md` from line 1. Read a section with `python3 .agents/scripts/spec_section.py <spec> <section>`:
- **C & Assembly AST**: `parser/c_ast/CAST_API.md` (§1, §2, §7, §7.1, §7.3, §9, §11), `parser/asm_ast/ASM_API.md`
- **Kconfig AST**: `parser/kconfig_ast/KCONFIG_API.md` (§1, §2, §6, §7, §8)
- **Rust AST**: `parser/rust_ast/RUST_API.md` (§1, §4, §8)
- **Maintainers**: `parser/maintainer_ast/MAINTAINER_API.md` (§1, §2, §5, §6)
- **Git Commit AST**: `parser/git_ast/GIT_API.md` (§1, §2, §4, §6)
- **Table Engine**: `table_engine/TE_API.md` (§1, §4, §6)

## 2. Parser Subsystem Fast Lookup & Test Map

| Subsystem | Source Directory | Spec Reference | Targeted Unit Test | Snapshot Regeneration Command |
| :--- | :--- | :--- | :--- | :--- |
| **C / ASM AST** | `parser/c_ast/`, `parser/asm_ast/` | `CAST_API.md`, `ASM_API.md` | `python3 -m unittest tests.test_c_ast unit_test.test_c_parser` | `UPDATE_SNAPSHOTS=1 python3 -m unittest unit_test.test_c_parser` |
| **Kconfig / Kbuild** | `parser/kconfig_ast/`, `parser/kbuild_parser.py` | `KCONFIG_API.md` | `python3 -m unittest tests.test_kconfig_ast tests.test_kconfig_engine` | `UPDATE_SNAPSHOTS=1 python3 -m unittest unit_test.test_kconfig_parser` |
| **Rust AST** | `parser/rust_ast/` | `RUST_API.md` | `python3 -m unittest tests.test_rust_ast` | `REGEN_GOLDEN=1 python3 -m unittest tests.test_rust_ast` |
| **Git Commit AST** | `parser/git_ast/` | `GIT_API.md` | `python3 -m unittest tests.test_git_commit_parser tests.test_commit_pipeline` | `UPDATE_SNAPSHOTS=1 python3 -m unittest unit_test.test_git_parser` |
| **Maintainers** | `parser/maintainer_ast/` | `MAINTAINER_API.md` | `python3 -m unittest tests.test_maintainer_ast tests.test_maintainer_files` | `UPDATE_SNAPSHOTS=1 python3 -m unittest unit_test.test_maintainer_parser` |
| **Man Page AST** | `parser/man_ast/` | `MAN_API.md` | `python3 -m unittest tests.test_man_ast` | — (no snapshots) |
| **Raw / Fallback AST**| `parser/raw_ast/` | `RAW_AST_API.md` | `python3 -m unittest tests.test_raw_ast unit_test.test_raw_parser` | `UPDATE_SNAPSHOTS=1 python3 -m unittest unit_test.test_raw_parser` |
| **Table Engine** | `table_engine/` | `table_engine/TE_API.md` | `python3 -m unittest unit_test.test_table_engine unit_test.test_table_handling` | N/A |

## 3. Snapshot Update & Verification Workflow
Enforce the staging invariants loaded from `parser/AGENTS.md` (Rule 12 tag staging order, Rule 14 binary digests, Rule 21 view parameter flattening, Rule 23 AST deduplication via `m_ast.view`, latin-1 source decoding, hermetic TU caches):
1. **Targeted Unit Test**:
   - Run the targeted test module from column 4 of the lookup table: `python3 -m unittest <targeted_test>`.
2. **Inspect Snapshot Discrepancies**:
   - If a snapshot test fails, carefully inspect the diff to verify that new tag, AST, or symbol rows are intentional.
3. **Regenerate Golden Snapshots**:
   - Only regenerate snapshots using the command in column 5 if the change is deliberate and verified.
4. **Documentation Sync**:
   - Update the subsystem's `*_API.md` file in declarative present-tense.
5. **Final Gate**:
   - Run `python3 main.py -u` once.
