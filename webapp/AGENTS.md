# WebApp Subsystem Rules (`webapp/`)

> [!TIP] Workflow Skill
> For full endpoint/view checklists, service mappings, and test runner commands, use `.agents/skills/add-webapp-endpoint/SKILL.md`.

Always enforce these invariants when modifying `webapp/backend/` or `webapp/frontend/`:

1. **Strict Read-Only Database & `db_engine` Routing (`webapp/API.md` §2.5)**:
   - Never execute DDL (`CREATE`, `ALTER`, `DROP`, `TRUNCATE`) or DML (`INSERT`, `UPDATE`, `DELETE`, `REPLACE`) in `webapp/`.
   - All database queries and connection handling MUST route through the `db_engine` abstraction (`BaseDBEngine`). Bypassing `db_engine` via direct `mysql.connector` driver calls or raw cursor execution outside `db_engine/` is strictly prohibited.
   - If a new or modified `webapp/` feature requires extra database writes, schema changes, or heavy source/commit parsing, **propose a change to the parser** (`main.py`, `core/DBLayout.py`, `parser/`, `core/backfills.py`) instead of implementing webapp-side parsing or DB writes.
   - Prioritize pushing feature extraction and persistence into the parser and querying the database in `webapp/` for parsed versions, leaving a `"git"` fallback in `webapp/` (reusing pure `parser/` extractors where possible) only for revisions not yet parsed.
2. **Zero-Trust Input & Output Sanitization (`webapp/API.md` §2.1–2.6)**:
   - **Git revisions & specs**: Validate every revision argument with `safe_rev()` / `is_safe_rev()` (`webapp/backend/security/git.py`) before passing to `git`, and every `<rev>:<path>` `cat-file` spec with `is_safe_git_spec()`.
   - **Paths**: Validate filesystem paths with `resolve_and_verify_repo_path()` (`webapp/backend/security/jail.py`) and grep pathspecs with `validate_pathspecs()`.
   - **SQL & Identifiers**: Escape `LIKE` patterns with `escape_like()` / `sanitize_like_query()` and validate grep/symbol identifiers with `is_valid_identifier()` (`webapp/backend/security/sql.py`).
   - **HTML DOM**: Escape every interpolated string in `innerHTML` / `insertAdjacentHTML` with `escapeHtml()` (`webapp/frontend/js/components/modal.js`). No inline scripts or `eval`.
3. **Bounded Multi-Repo Caching (`webapp/API.md` §2.10)**: Scope caches with `RepoScopedCache(maxsize=...)` whenever keys include caller-supplied parameters; read once with `.get()` into a local variable.
4. **No Unrequested Change Without Confirmation (`webapp/API_FRONTEND.md` §5.0)**: Any frontend action whose effect reaches beyond the change the user directly requested (other Kconfig symbol values, dependency auto-solves, state resets) must compute the full consequence, show a preview dialog (`change_preview.js`), and apply only on explicit confirmation. Dismissing leaves state untouched; no dialog is shown when nothing else changes.
5. **Documentation Split**: Update `webapp/API.md` for core/security/service architecture (§1–3, §6–8), `webapp/API_ENDPOINTS.md` for REST endpoints (§4), and `webapp/API_FRONTEND.md` for frontend modules and UI views (§5).

