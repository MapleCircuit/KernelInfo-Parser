---
name: parser-snapshot-update
description: >-
  Workflow for modifying an AST or file parser in parser/, running targeted parser
  unit tests, and verifying or regenerating JSON golden snapshots.
---

# Modify an AST Parser & Verify Snapshots

## 1. Targeted Spec Slice Lookup
Never read `*_API.md` from line 1. Jump directly to the relevant slice or run `python3 .agents/scripts/spec_section.py <spec> <section>`:
- **C & Assembly AST**: `parser/c_ast/CAST_API.md` (§1 `[L24-L104]`, §2 `[L105-L232]`, §7 `[L623-L1030]` / §7.1 `[L625-L724]` / §7.3 `[L725-L1030]`, §9 `[L1097-L1180]`, §11 `[L1230-L1278]`), `parser/asm_ast/ASM_API.md`
- **Kconfig AST**: `parser/kconfig_ast/KCONFIG_API.md` (§1 `[L23-L46]`, §2 `[L47-L66]`, §6 `[L112-L172]`, §7 `[L173-L180]`, §8 `[L181-L262]`)
- **Rust AST**: `parser/rust_ast/RUST_API.md` (§1 `[L21-L63]`, §4 `[L132-L170]`, §8 `[L254-L267]`)
- **Maintainers**: `parser/maintainer_ast/MAINTAINER_API.md` (§1 `[L22-L71]`, §2 `[L72-L148]`, §5 `[L197-L247]`, §6 `[L248-L287]`)
- **Git Commit AST**: `parser/git_ast/GIT_API.md` (§1 `[L20-L63]`, §2 `[L64-L116]`, §4 `[L157-L182]`, §6 `[L217-L284]`)
- **Table Engine**: `table_engine/TE_API.md` (§1 `[L18-L59]`, §4 `[L211-L530]`, §6 `[L551-L641]`)

## 2. Parser Subsystem Fast Lookup & Test Map

| Subsystem | Source Directory | Spec Reference | Targeted Unit Test | Snapshot Regeneration Command |
| :--- | :--- | :--- | :--- | :--- |
| **C / ASM AST** | `parser/c_ast/`, `parser/asm_ast/` | `CAST_API.md` `[L24-L104]`, `ASM_API.md` | `python3 -m unittest tests.test_c_ast unit_test.test_c_parser` | `UPDATE_SNAPSHOTS=1 python3 -m unittest unit_test.test_c_parser` |
| **Kconfig / Kbuild** | `parser/kconfig_ast/`, `parser/kbuild_parser.py` | `KCONFIG_API.md` `[L23-L46]` | `python3 -m unittest tests.test_kconfig_ast tests.test_kconfig_engine` | `UPDATE_SNAPSHOTS=1 python3 -m unittest unit_test.test_kconfig_parser` |
| **Rust AST** | `parser/rust_ast/` | `RUST_API.md` `[L21-L63]` | `python3 -m unittest tests.test_rust_ast` | `REGEN_GOLDEN=1 python3 -m unittest tests.test_rust_ast` |
| **Git Commit AST** | `parser/git_ast/` | `GIT_API.md` `[L20-L63]` | `python3 -m unittest tests.test_git_commit_parser tests.test_commit_pipeline` | `UPDATE_SNAPSHOTS=1 python3 -m unittest unit_test.test_git_parser` |
| **Maintainers** | `parser/maintainer_ast/` | `MAINTAINER_API.md` `[L22-L71]` | `python3 -m unittest tests.test_maintainer_ast tests.test_maintainer_files` | `UPDATE_SNAPSHOTS=1 python3 -m unittest unit_test.test_maintainer_parser` |
| **Raw / Fallback AST**| `parser/raw_ast/` | `RAW_AST_API.md` | `python3 -m unittest tests.test_raw_ast unit_test.test_raw_parser` | `UPDATE_SNAPSHOTS=1 python3 -m unittest unit_test.test_raw_parser` |
| **Table Engine** | `table_engine/` | `table_engine/TE_API.md` `[L18-L59]` | `python3 -m unittest unit_test.test_table_engine unit_test.test_table_handling` | N/A |

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
