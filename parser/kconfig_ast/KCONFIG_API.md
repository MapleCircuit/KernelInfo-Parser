# Linux Kconfig & Kbuild Subsystem API & Architectural Contract Specification (`kconfig_ast`)

Authoritative architectural specification and relational staging reference for `parser/kconfig_ast/` ([kconfig_ast.py](parser/kconfig_ast/kconfig_ast.py), [kconfig_lexer.py](parser/kconfig_ast/kconfig_lexer.py), [kconfig_parser.py](parser/kconfig_ast/kconfig_parser.py), [kconfig_model.py](parser/kconfig_ast/kconfig_model.py), [kconfig_eval.py](parser/kconfig_ast/kconfig_eval.py)) and the associated build system parsers [kbuild_parser.py](parser/kbuild_parser.py) and [kbuild_conditions.py](parser/kbuild_conditions.py). Designed as an exhaustive reference for AI agents extending, maintaining, or querying the Linux kernel configuration and build dependency graph.

Two consumers share the lexer/parser:
- **Database pipeline** (`kconfig_ast.py`): per-file AST, tags and relational rows (sections 1-4).
- **Semantic model + evaluator** (`kconfig_model.py`, `kconfig_eval.py`, section 8): a port of `scripts/kconfig` used by the web application's configuration engine. It reproduces the kernel's own `conf` output byte for byte.

---

## 1. High-Level Architecture & Pipeline Flow

The Kconfig subsystem extracts configuration symbol definitions, dependency expressions (`depends on`), reverse dependencies (`select`, `imply`), menu tree hierarchies, and compiled object bindings into relational database tables.

```
==================================================================================================
KCONFIG & KBUILD PARSING PIPELINE
--------------------------------------------------------------------------------------------------
Kconfig Source File (Kconfig*)
  │
  ├─ Git Operation: "R100" (Exact Rename) ──► No-op (Preserves old blob and tags)
  │
  ├─ Git Operation: "D" (Deleted)
  │  ├─ get_prior_tags(CS) ──► Queries old_vid tags via 3-way join (m_bridge_tag -> m_tag -> m_ast)
  │  └─ close_prior_tags(CS) ─► No-op under immutable Git DAG architecture
  │
  └─ Git Operation: "A" / "M" / "R*" (Added / Modified / Content Rename)
     ├─ get_prior_tags(CS) (if M or R*)
     │
     ▼ [KconfigManager (kconfig_ast.py)]
Lexical Scanning & Recursive Descent Grammar:
  ├── KconfigLexer (kconfig_lexer.py): Slices source into tokens (keywords, symbols, strings, prompts, help blocks)
  └── KconfigParser (kconfig_parser.py): Builds typed node tree
        ├── Symbols: KconfigConfig (config, menuconfig)
        ├── Menus & Choices: KconfigMenu, KconfigChoice
        ├── Conditionals: KconfigIf
        ├── Sources: KconfigSource (source "path")
        ├── Comments: KconfigComment
        └── Boolean Logic: KconfigExpr (AND, OR, NOT, EQUAL, UNEQUAL)
  │
  ▼ [KconfigManager.extract_item() -> Relational ChangeSet Staging]
Relational Database Mapping:
  ├── AST Symbol Staging: m_ast.view [Rule 23]
  ├── Core Code Tag Staging: m_tag.set (5-col, Rule 12 order), m_tag_code, m_bridge_tag (6-col), m_map_ast, m_bridge_map
  ├── Symbol Registry: m_kconfig_symbol (Table 16, 7-col content deduplicated)
  ├── Dependency & Reverse-Dependency Graph: m_kconfig_relation (Table 17, 6-col)
  └── Menuconfig UI Hierarchy: m_kconfig_tree (Table 18, 9-col blob-scoped)
  │
  ▼ [Post-Processing: processing_kbuild(version) (main.py + kbuild_parser.py)]
Kbuild / Makefile Integration:
  ├── KbuildParser: Scans all Makefiles & Kbuild files in the kernel tree
  ├── Extracts bindings: obj-$(CONFIG_XYZ) += target.o, composite targets (target-objs := a.o b.o)
  └── Staging: m_kconfig_kbuild (Table 19) links kcid -> (makefile_blob_id, target_fnid, compile_mode, target_obj)
==================================================================================================
```

---

## 2. Relational Schema Mapping & ChangeSet Operations

`kconfig_ast` coordinates between general AST tracking tables (0–15) and Kconfig-specific domain tables (16–19):

### 2.1. Core AST and Tag Staging

1. **AST Node Definition (`m_ast.view` per Rule 23)**:
   ```python
   with CS(REF_POS):
       CS.store(m_ast.view(
           ((m_ast.ast_id,),),
           None,
           symbol_name[:255],
           int(ast_type),
       ))
       ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
   ```

2. **Occurrence Tagging & Deduplication (Rule 12 Order)**:
   `m_tag.set` **must be the first operation** inside `with CS(REF_POS):` using the zero-VID 5-column schema:
   ```python
   with CS(REF_POS):
       CS.store(m_tag.set(
           None,            # tag_id (AUTO_INCREMENT)
           code_hash,       # 32-byte binary SHA-256 digest
           ast_ref,         # reference to m_ast node
           0,               # hl_s
           0,               # hl_l
       ))
       tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
       CS.store(m_tag_code.get_set(code_hash, extent.code))
       if s_tag_id is not None:
           CS.store(m_moved_tag.set(s_tag_id, tag_ref))
   ```

3. **Spatial Mapping & Blob Bridging**:
   ```python
   with CS(REF_POS):
       CS.store(m_bridge_tag.set(
           ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
           tag_ref,
           extent.line_pos[0],
           extent.line_pos[1],
           extent.char_pos[0],
           extent.char_pos[1],
       ))
   CS.store(m_map_ast.set(tag_ref, 1, 1, rel_line_e, rel_char_e, ast_ref))
   CS.store(m_bridge_map.set(tag_ref, tag_ref))
   ```

### 2.2. Domain Tables (Tables 16, 17, 18, 19)

| Table ID | Table Name | Columns | Primary Key | `no_duplicate` | Description |
| :---: | :--- | :--- | :--- | :---: | :--- |
| **16** | `m_kconfig_symbol` | `(kcid, name, type, prompt, def_val, help, ast_id)` | `("kcid",)` | `True` | Content-deduplicated Kconfig symbol definitions. Type codes: 1=bool, 2=tristate, 3=string, 4=hex, 5=int, 0=unknown. |
| **17** | `m_kconfig_relation` | `(rel_id, kcid, target_name, rel_type, cond_ast_id, priority)` | `("rel_id",)` | `True` | Direct dependencies & reverse dependencies. Relation types: `1=depends_on`, `2=select`, `3=imply`, `4=choice_member` (`target_name` = the enclosing choice's name, or its prompt for anonymous choices; also emitted for members nested in `if` blocks). |
| **18** | `m_kconfig_tree` | `(tree_id, blob_id, parent_id, node_type, title, kcid, priority, dep_ast_id, ast_id)` | `("tree_id",)` | `False` | Hierarchical Menuconfig tree scoped to `blob_id`. Node types: `1=menu`, `2=choice`, `3=config`, `4=menuconfig`, `5=comment`. `dep_ast_id` is the entry's whole effective dependency within the file: the AND of inherited (`menu`/`if`/`choice`) and own `depends on` expressions. |
| **19** | `m_kconfig_kbuild` | `(kcid, makefile_blob_id, target_fnid, compile_mode, target_obj)` | `("kcid", "makefile_blob_id", "target_fnid", "compile_mode", "target_obj")` | `False` | Compiled file & object mappings scoped to Makefile blobs. Compile modes: `1=built-in` (`y`), `2=module` (`m`), `3=conditional` (`y/m`). |

#### Example: Staging `m_kconfig_relation`
```python
with CS(REF_POS):
    CS.store(m_kconfig_relation.set(
        None,             # rel_id (AUTO_INCREMENT, PK)
        kcid_ref,         # source symbol kcid reference
        target_sym_name,  # target symbol identifier (e.g. "BLOCK", "NET")
        1,                # rel_type: 1 = depends_on
        cond_ast_ref,     # conditional AST expression reference or 0
        rel_priority,     # sequence priority
    ))
```

---

## 3. AST Node Classification & Grammar

The parser implements a robust grammar for Linux Kconfig constructs:

| Grammar Node | `ASTT` Category | Numeric ID | Responsibilities |
| :--- | :--- | :---: | :--- |
| **`KconfigConfig`** | `ASTT.Kconfig_Config` | 88 | `config <NAME>` or `menuconfig <NAME>`. Captures symbol type (`bool`, `tristate`, `int`, `hex`, `string`), prompt string, default values, `depends on` clauses, `select` clauses, `imply` clauses, and multi-line `help` documentation. |
| **`KconfigMenu`** | `ASTT.Kconfig_Menu` | 89 | `menu "<TITLE>" ... endmenu`. Hierarchical menu container grouping configuration symbols. |
| **`KconfigChoice`** | `ASTT.Kconfig_Choice` | 90 | `choice ... endchoice`. Mutual-exclusion selection block among child symbols. |
| **`KconfigIf`** | `ASTT.Kconfig_If` | 91 | `if <EXPR> ... endif`. Scoped conditional block propagating parent dependencies to nested constructs. |
| **`KconfigSource`** | `ASTT.Kconfig_Source`, `ASTT.Kconfig_Rsource`, `ASTT.Kconfig_Osource` | 92, 93, 188 | Dynamic include directives (`source "<PATH>"`, relative `rsource`, optional `osource`) linking child Kconfig files. |
| **`KconfigComment`** | `ASTT.Kconfig_Comment` | 93 | `comment "<TITLE>"`. Non-configurable section heading displayed in menuconfig. |
| **`KconfigMainmenu`** | `ASTT.Kconfig_Mainmenu` | 94 | `mainmenu "<TITLE>"`. Root title declaration for kernel configuration. |
| **`KconfigExpr`** | Multiple | 95–99 | Boolean logic tree: `ASTT.Kconfig_Op_And` (95), `ASTT.Kconfig_Op_Or` (96), `ASTT.Kconfig_Op_Not` (97), `ASTT.Kconfig_Op_Equal` (98), `ASTT.Kconfig_Op_Unequal` (99). Relational operators `<`, `<=`, `>`, `>=` (Linux 4.x+) are staged as `ASTT.Undefined` nodes named by the operator text. |

### 3.1. Lexer and Grammar Details

- **Keywords are case-sensitive** (as in `zconf.gperf`): `Bool` or `HELP` are symbol names, not keywords.
- **Operators**: `&&`, `||`, `!`, `=`, `!=`, `<`, `<=`, `>`, `>=`, parentheses. Precedence: `||` < `&&` < `!` < comparison < primary.
- **Macro language** (Linux 4.18+): with a `KconfigPreprocessor` (section 8.6), `KconfigLexer` executes `name := value` / `name = value` / `name += value` lines and expands `$(...)` while lexing, exactly where kconfig does: a token containing `$` becomes one `SYMBOL` token (empty results vanish), quoted strings are expanded in place, help texts are not. `iter_tokens()` lexes lazily so a sourced file is read before the rest of its parent (`KconfigParser(on_source=...)` fills `KconfigSource.items`/`resolved_path`). Without a preprocessor (older trees, DB pipeline) a `$(...)` reference is one `MACRO` token parsed as a `CONSTANT` holding the raw text.
- **Constants vs symbols**: quoted words are `ExprOp.CONSTANT`. Bare words (`y`, `0x1000`, `-1`, `FOO`) are `ExprOp.SYMBOL_REF`; kconfig treats an undefined word as a constant whose value is its own name.
- **`KconfigConfig` properties**: `sym_type`, `prompt`/`prompt_cond`, `defaults` (value, condition) in source order, `depends_on`, `selects`, `implies`, `ranges` (min/max as expressions, so bounds may name other int/hex symbols), `help_text`, raw `options` plus structured `env` (`option env="X"`), `is_modules` (`option modules` or `modules`), `is_defconfig_list`, `allnoconfig_y`.
- **Unknown property lines** are skipped whole, so a trailing `if` is never taken for an `if` block.
- **`KconfigChoice.sym_type`** stays `TYPE_UNKNOWN` when the choice declares no type; kconfig then uses the first typed member's type.
- **`KconfigMenu.visible_if`**: successive `visible if` lines are ANDed.

---

## 4. Tag Lifecycle & Content Addressable Deduplication

Kconfig files track tag continuity across kernel releases using `c_ast` tag tracking utilities ([tag_tracker.py](parser/c_ast/tag_tracker.py)):

1. **Tag Hash Deduplication**:
   Exact binary SHA-256 matches recycle existing `tag_id`s, preserving database space and preventing duplicate rows in `m_tag`.
2. **Prior Tag Evolution (`match_prior_tag_transition`)**:
   When a Kconfig symbol's help text, prompt, or default values change between kernel versions, the transition engine detects the modified symbol and stages `m_moved_tag(s_tag_id, tag_ref)`.
3. **Symbol Lifecycle (`m_kconfig_symbol`)**:
   Symbols are content-deduplicated globally (`no_duplicate=True`) without version columns. Blob-specific hierarchy binds each menu node to its containing file via `m_kconfig_tree.blob_id`.
4. **Immutable Tag Closing**:
   Under the Git DAG architecture, prior tags are immutable; `close_prior_tags(CS)` operates as a safe no-op.

---

## 5. Cross-File Reference Handling

1. **`source` Directive Resolution**:
   - `KconfigSource` extracts the target path (e.g. `source "drivers/net/Kconfig"`).
   - Stages `m_file_name.get_set(None, norm_path)` and links the dependency via `m_ast.view` joined with `m_ast_include`.
2. **Symbol Cross-Referencing**:
   - Symbols referenced in `depends on`, `select`, or `imply` statements are stored by name in `m_kconfig_relation.target_name`.
   - TableEngine queries can join `m_kconfig_relation.target_name` against `m_kconfig_symbol.name` to traverse the full directed acyclic dependency graph.

---

## 6. Dedicated Kbuild Subsystem Section (`kbuild_parser.py`)

The Kbuild parser maps Kconfig configuration symbols to concrete C, ASM, and object source files across the kernel directory tree:

### 6.1. Architecture & Execution
- **Location**: [parser/kbuild_parser.py](parser/kbuild_parser.py)
- **Invoked by**: `main.py:processing_kbuild(version)` at Step 6.6 of the update pipeline.
- **Workflow**:
  1. Traverses the kernel source tree for all `Makefile` and `Kbuild` files.
  2. Joins backslash line continuations (`\`) into logical lines.
  3. Evaluates regexes:
     - `OBJ_ASSIGN_RE`: Matches `obj-$(CONFIG_XYZ) += target.o`, `obj-y += ...`, `obj-m += ...`.
     - `COMPOSITE_RE`: Matches composite object rules `target-y := a.o b.o`, `target-objs := ...`.
  4. Resolves `.o` object goals to underlying `.c` and `.S` source files.
  5. Maps relative file paths to `target_fnid` in `m_file_name`.
  6. Queries `m_kconfig_symbol` for `kcid` by symbol name.
  7. Stages batches into `m_kconfig_kbuild.set(kcid, makefile_blob_id, target_fnid, compile_mode, target_obj)`.

### 6.2. Compile Modes
- `compile_mode = 1`: Built-in compilation (`y` / `obj-y`).
- `compile_mode = 2`: Loadable module compilation (`m` / `obj-m`).
- `compile_mode = 3`: Configurable compilation (`y/m` depending on active kernel config).

---

### 6.3. Conditional Blocks, Goal Lists and Directories
- `ifeq`/`ifneq`/`ifdef`/`ifndef` blocks are tracked: every binding records the `CONFIG_*` symbols of its enclosing conditions in `KbuildBinding.cond_symbols` (`else` keeps the block's symbols). A rule like `ifeq ($(CONFIG_X86_32),y) obj-y += foo.o` is therefore attributed to `X86_32`.
- `obj-$(subst m,y,$(CONFIG_X))` is attributed to `X`.
- `lib-*` (relative to the Makefile) and the arch goal lists `core-*`, `drivers-*`, `libs-*`, `net-*`, `init-*`, `head-*` (relative to the source root) are parsed like `obj-*`.
- `obj-$(CONFIG_X) += dir/` produces a binding with `is_directory=True` (`source_file_rel` = `dir/Makefile`): the whole sub-directory is built when `X` is enabled.

### 6.4. Build Conditions (`kbuild_conditions.py`)
[parser/kbuild_conditions.py](parser/kbuild_conditions.py) answers "is this file compiled by `make ARCH=<arch>` under a given `.config`?" for the webapp (`GET /api/kconfig/{v}/build`, the code view's *not compiled* banner, and Go to Definition; see `webapp/API.md`). `KbuildParser` only records which symbols a rule mentions; this module keeps the full condition:
- **`KbuildConditionResolver(read, exists, arch, srcarch, included_sources)`** reads files of one revision through callbacks (no Git or database access of its own) and memoizes parsed Kbuild files and directory conditions.
- **Top level**: the goal lists (`core-*`, `drivers-*`, `net-*`, `init-*`, `libs-*`, `head-*`) of the top `Makefile` and `arch/<srcarch>/Makefile`, plus the root `Kbuild` file of newer kernels (`obj-y += init/ usr/ arch/$(SRCARCH)/ ...`), name the top directories. Branches on make-internal variables (`mixed-targets`, `KBUILD_EXTMOD`...) are transparent there, so the goal lists of the regular build branch apply.
- **Directories**: each directory's Kbuild file (`Kbuild` when present, else `Makefile`) descends with `obj-*/subdir-* += dir/` or (older kernels) `obj-* += dir/built-in.o`; a directory's condition is its rule AND its parent's condition.
- **Objects**: `obj-*`, `lib-*`, `extra-*`, `head-y` and composite members (`foo-y`, `foo-objs`, `foo-$(CONFIG_X)` of an `obj-* += foo.o`), list references (`bonding-objs += $(proc-y)`: the members of `proc-$(CONFIG_PROC_FS)` when it is `y`), `$(addprefix dir/, ...)`, and `sub/obj.o` named by an ancestor directory. Sources are `.c`, `.S` (also `*.lds.S`) and `.rs`.
- **Conditionals keep their polarity**: `ifeq`/`ifneq`/`ifdef`/`ifndef`/`else [if...]` become expressions (`ifeq ($(CONFIG_X86_32),y)` → `{"eq": [[{"val": "X86_32"}], ["y"]]}`, the `else` branch its negation), `define`/`endef` blocks are skipped.
- **Variables**: simple assignments of the Kbuild file and the variables the arch Makefile `export`s (e.g. `BITS := 32`/`64` under `ifeq ($(CONFIG_X86_32),y)`) are expanded per alternative (at most 4), each alternative carrying its condition: `obj-y += process_$(BITS).o` names `process_32.o` when `X86_32` is `y` and `process_64.o` otherwise. `ARCH` and `SRCARCH` are known.
- **Expressions** (JSON, evaluated client-side by `webapp/frontend/js/views/kconfig/build_condition.js`; `evaluate()` is the Python twin): `"y"`/`"m"`/`"n"`/`"?"`, `{"sym": X}`, `{"def": X}`, `{"eq": [a, b]}`, `{"not": e}`, `{"and": [...]}`, `{"or": [...]}` (multiple rules for one object are OR-ed), folded by `and_`/`or_`/`not_`. Evaluation is three-valued: `and` with a definite `n` is `n`, `or` with a definite `y`/`m` is that value, otherwise unknown propagates.
- **`file_condition(path)`** returns `{status, expr, rules, reason}`:
  - `other_arch`: under another architecture's `arch/` tree (ARCH=um also builds `arch/x86/`).
  - `rule`: Kbuild rules name the object (`rules`: its rule, then the directory chain).
  - `no_rule`: the directory is reachable, its Kbuild file is fully parsed and does not mention the object at all.
  - `unknown`: everything the resolver cannot prove: headers and non-Kbuild files, directories built by their own make targets (named in the top/arch Makefile, e.g. `arch/x86/boot`, or mentioned only by custom rules of the parent, e.g. `aicasm/`), objects mentioned by custom rules or lists (vdso objects, `targets`), host/user programs (`hostprogs`, `always`, `subdir-*`), lists computed by `$(shell)`/`$(wildcard)`, unexpandable references, Makefiles `include`-ing other Makefiles, and sources another file `#include`s (`included_sources`). Unknown is never reported as unbuilt.
- **Measured coverage** (x86_64 defconfig, `.c`/`.S` files): v3.0 resolves 1754 `y`, 3 `m`, 9164 `n`, 5703 other-arch, 22 `no_rule`, 785 unknown in ~4 s; v7.2 resolves all but ~6100 unknown, mostly `tools/`, `drivers/gpu/` (included sub-Makefiles), `samples/` and `arch/x86/boot`, in ~10 s (one full pass; the webapp only resolves requested files).

---

## 7. Performance Invariants & Architectural Rules

1. **Rule 12 Staging Order**:
   In `KconfigManager._stage_tag()`, `m_tag.set` MUST be the first operation inside `with CS(REF_POS):` so `tag_ref` references `m_tag`. Auxiliary tables (`m_tag_code.get_set`, `m_moved_tag.set`) follow immediately within the block.
2. **Rule 23 View Deduplication**:
   All AST node creation (`_extract_expr_ast`, `_stage_ast_node`) uses `m_ast.view(((m_ast.ast_id,),), None, name, type_id)`.
3. **Expression Tree Flattening**:
   Deep boolean expressions (e.g. chained `A && B && C && D`) are evaluated iteratively to prevent Python `RecursionError` exceptions on complex platform dependencies.
4. **Zero-Duplication Kbuild Ingestion**:
   `m_kconfig_kbuild` uses primary key `("kcid", "makefile_blob_id", "target_fnid", "compile_mode")` with direct TableEngine caching to prevent redundant insertions across multi-directory Makefiles.

---

## 8. Semantic Model & Evaluator (`kconfig_model.py`, `kconfig_eval.py`)

The web configuration engine needs kconfig's *semantics*, not only its syntax. These two modules port `scripts/kconfig` (`zconf.y` parse actions, `menu.c:menu_finalize`, `symbol.c`, `confdata.c`).

### 8.1. Building a Model
```python
from parser.kconfig_ast.kconfig_model import KconfigModelBuilder, KconfigEnv
builder = KconfigModelBuilder(read_file, KconfigEnv(ARCH="x86_64", SRCARCH="x86", KERNELVERSION="3.0.0"))
model = builder.build("Kconfig")  # root: top-level Kconfig (arch/$SRCARCH/Kconfig on very old trees)
```
`read_file(path) -> str | None` returns a file of the revision being modelled (the web app reads Git objects). The builder:
1. **Sources** files recursively, expanding `$VAR` from the environment (`source "arch/$SRCARCH/Kconfig"`); `rsource` is relative to the current file, `osource` may be missing. A file may be sourced more than once (each inclusion creates entries); only recursive inclusion is refused.
2. **Applies parse actions**: one menu entry per `config`/`menuconfig`/`menu`/`choice`/`comment`/`if`; properties are attached to the entry where they are written; `depends on` accumulates in the entry's dependency; `visible if` of every enclosing menu is ANDed into config prompts; every dependency and property condition has `m` rewritten to `m && <modules symbol>` (the symbol with `option modules`, else `MODULES`); `option env="X"` makes the symbol automatic (never written) with the environment value as default.
3. **Finalizes** (`menu_finalize`): parent dependencies flow into each entry and each of its properties; `select`/`imply` build reverse dependencies (`target.rev_dep |= owner && condition`); `FOO!=n` is rewritten to `FOO` for tristate `FOO` in properties of non-tristate symbols (`expr_trans_bool`); choices take the first typed member's type and collect their members; values of tristate choices that are bool depend on `choice=y`; entries depending on the preceding symbol become its children (implicit submenus); prompt-less entries (`if` blocks) hand their children to their parent; non-optional choices get `rev_dep = prompt && m`.

### 8.2. Model Format
Expressions are hash-consed into `model["exprs"]` (shared parent dependency chains stay small). Ids index the table, `-1` means "no expression" (always y):
`["s", name]` (word: y/m/n, defined or undefined symbol), `["c", text]` (quoted constant), `["&", a, b]`, `["|", a, b]`, `["!", a]`, `["=", a, b]`, `["!=", a, b]`, `["<"|"<="|">"|">=", a, b]`.

- `model["symbols"][name]`: `type`, `prompts` (visibility expression of each prompt), `defaults` (`[value, condition]` in kconfig order across all definitions), `ranges`, `selects`/`implies` (`[target, condition]`), `rev_dep`, `implied`, `dir_dep`, `menus` (definition entries), plus `choice` (for choice values), `is_choice`/`optional`/`members` (choices, keyed `<choice-N>` or `<choice:NAME>`), `auto`, `env`.
- `model["nodes"]`: entries in menu order (pre-order, root first): `tree_id`, `parent_id`, `kind` (`root`/`menu`/`choice`/`config`/`menuconfig`/`comment`/`if`), `node_type`, `symbol_name`, `prompt`, `pvis` (prompt visibility), `vis_if`, `dep`, `file_path`, `line_s`, `line_e`, `has_help`, `children`.
- `env`, `mainmenu` (expanded), `modules_sym`, `defconfig_list`, `files` (every Kconfig file read), `warnings`.
- `builder.references[name]`: the direct references made by entries' own properties (`depends_on`, `select`, `imply`, `default`, `prompt_condition`, ...) with file and line. `builder.help_texts()` and `builder.menu_help()` hold the help texts kept out of the model.
- `compact_model(model)` is the transport form served to the browser (short keys, menu rows as arrays, interned file paths).

### 8.3. Evaluating
```python
from parser.kconfig_ast.kconfig_eval import KconfigEvaluator, solve
ev = KconfigEvaluator(model)
report = ev.load_config(defconfig_text)   # conf_read: {"unknown", "invalid", "warnings", "unsaved"}
ev.set_all_new_symbols("default")         # conf_set_all_new_symbols ("yes"/"mod"/"no" = all*config)
ev.value("SMP"); ev.visibility("SMP"); ev.rev_dep("SMP")
ev.set_value("NR_CPUS", "8")              # sym_set_string_value: False when kconfig would refuse it
ev.toggle("EXT4_FS")                       # sym_toggle_tristate_value
ev.write_config()                          # conf_write: full .config with menu comment blocks
ev.write_defconfig()                       # conf_write_defconfig: make savedefconfig
solve(ev, "KVM_INTEL")                     # legal changes reaching the target (Auto-solve)
```
Values are computed lazily and memoized until the next change (kconfig's `SYMBOL_VALID`); a symbol read while it is being computed yields its provisional value, as in kconfig. Only legal changes are accepted by the setters: tristate values must lie between the reverse dependency and the prompt visibility, `m` needs modules, choice values are changed by selecting another member, and int/hex values must be valid and inside the active `range`.

`webapp/frontend/js/views/kconfig/kconfig_engine.js` is the client-side twin (same algorithm, same model); keep both in sync.

### 8.4. Fidelity and Limits
- Verified byte for byte against the real `conf` of Linux v3.0 (x86_64, i386, arm, sparc, mips, powerpc defconfigs, allno/allyes/allmodconfig, savedefconfig), v4.15 (x86_64, arm64, allmodconfig) and v6.8 (x86_64_defconfig with the macro language, once with gcc 13.3.0 + binutils 2.42 and once with clang 18.1.3, reproduced from toolchain profiles). Fixtures and tests: `tests/test_kconfig_engine.py`, `tests/fixtures/kconfig/`. Configurations produced by random sequences of legal edits are fixed points of `conf --oldnoconfig`.
- `conf` behaviours that changed over time are detected from the revision's own `scripts/kconfig` sources (`model["semantics"]`): `write_on_nonzero_default` (a default evaluating to n no longer writes "is not set"), `menu_end_comments` (`# end of <menu>` lines), `int_hex_zero_default` (int/hex without value are `0`/`0x0`), `tristate_choice_mod_hidden` (m-visible tristate values of a y choice are hidden). `model["config_header"]` holds the header wording of that revision.
- Toolchain answers come from a profile (section 8.6), not a real compiler: identity-derived answers (versions, `CC_IS_*`, `LD_IS_*`, minimum versions) are exact; feature checks use a table of known first gcc/clang versions, and anything else is assumed to succeed. Every answer is reported with its source so it can be overridden.
- `dir_dep` uses the modern OR-of-definitions rule (v3.0 kept the last definition's); it only affects `imply` (absent in v3.0) and unmet-dependency warnings.
- Range bounds are evaluated as 64-bit integers (Linux 4.x+ `strtoll`); v3.0 used 32-bit `int`, which only differs for out-of-range values beyond 2^31.

### 8.5. Architecture Environment Helpers
- `arch_aliases(top_makefile)`: ARCH values remapped to another SRCARCH (`i386`/`x86_64` -> `x86`, `sparc32`/`sparc64` -> `sparc`, `sh64` -> `sh`, ...).
- `kernel_version_string(top_makefile)`: `KERNELVERSION` as the top Makefile computes it.
- `resolve_kbuild_defconfig(arch_makefile, arch, srcarch)`: the `KBUILD_DEFCONFIG` that `make ARCH=<arch> defconfig` loads, evaluating `ifeq`/`ifdef` blocks with ARCH/SRCARCH known, every `CONFIG_*` undefined (no `.config` is read for configuration targets) and `CROSS_COMPILE` set.

### 8.6. Macro Language and Toolchain (`kconfig_preprocess.py`, `kconfig_toolchain.py`)
Linux 4.18+ Kconfig asks the build environment questions while it is parsed (`scripts/Kconfig.include`: `$(cc-option,...)`, `$(ld-option,...)`, `$(as-instr,...)`, `$(success,...)`, `cc-version.sh`, ...). Pass a preprocessor to the builder when the revision has `scripts/kconfig/preprocess.c`:
```python
from parser.kconfig_ast.kconfig_preprocess import KconfigPreprocessor
from parser.kconfig_ast.kconfig_toolchain import ToolchainProfile, ToolchainShell
profile = ToolchainProfile.from_dict({"cc": "clang", "cc_version": "18.1.3", "probes": {"cc-option:-fsanitize=kcfi": "n"}}, "6.8.0")
env = profile.environment("x86_64", "x86", "6.8.0")      # CC, LD, CLANG_FLAGS, CC_VERSION_TEXT, ... + profile.env overrides
shell = ToolchainShell(profile, env, "x86", read_file)
pp = KconfigPreprocessor(env, shell); shell.pp = pp       # pp records env_used, shell_calls, messages
model = KconfigModelBuilder(read_file, KconfigEnv("x86_64", "x86", "6.8.0", extra=env), pp).build("Kconfig")
```
- **`KconfigPreprocessor`** (port of `preprocess.c`): simple (`:=`), recursive (`=`) and append (`+=`) variables, `$(1)`... arguments, built-ins `shell`, `info`, `warning-if`, `error-if`, `filename`, `lineno`, then environment variables. `error-if`/`warning-if`/`info` are recorded in `messages` (the real kconfig exits on `error-if`; here parsing continues so the problem can be shown). `env_used` lists every environment variable read.
- **`ToolchainProfile`**: `cc` (gcc|clang), `cc_version`, `ld` (bfd|lld), `ld_version`, `assembler` (gnu|llvm), `binutils_version`, `cross_compile`, `llvm_utils`, `pahole_version` (`"none"` = not installed), `rustc_version`, `gcc_plugins`, `host_release` (`uname -r`), `env` (overrides of any environment variable) and `probes` (overrides of probe answers). Missing versions default to a toolchain contemporary with the kernel release.
- **`ToolchainShell`** interprets the shell subset Kconfig uses (`{ }`, `&&`, `||`, `;`, pipes, redirections, `test`, `echo`, `printf`, `set --`, `grep`, `head`, `sed`, `cut`, `command -v`, `env`, `uname`) and classifies external commands into probes keyed for overrides: `cc-option:<flags>`, `ld-option:<flags>`, `as-instr:<instruction>`, `cc-snippet:<code>`, `script:<name> [args]`, `file:<path>`, `cc-print:<file>`, `cmd:<command>`. Version scripts (`cc-version.sh`, `gcc-version.sh`, `clang-version.sh`, `ld-version.sh`, `lld-version.sh`, `as-version.sh`, `pahole-version.sh`, `rustc-version.sh`) print the format of the revision's own script and honour `scripts/min-tool-version.sh`. `probes` maps each key to `{key, kind, label, command, result, source: toolchain|knowledge|assumed|override, note, locations}`.
