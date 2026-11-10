#!/usr/bin/env python3
"""pre_tool_guard.py - PreToolUse hook enforcing token-saving guardrails & slice reads.

Invoked by Antigravity lifecycle hooks (`.agents/hooks.json`, tool `read_file`) and by
Claude Code PreToolUse hooks (`.claude/settings.json`, tool `Read`).
Receives hook payload JSON on stdin, outputs decision JSON on stdout.
"""

import json
import sys
import os
from pathlib import Path

# Max allowed slice span (lines) for large files (>40 KB)
MAX_SLICE_SPAN = 350
LARGE_FILE_THRESHOLD_BYTES = 40_000

# Forbidden exact basenames
FORBIDDEN_BASENAMES = {
    "dockerfile",
    "install.md",
    "stringwrangler.py",
    ".gitignore",
    "ruff.toml",
}

# Allowed exceptions inside db_engine/
ALLOWED_DB_ENGINE_FILES = {
    "__init__.py",
    "base.py",
    "db_api.md",
    "dbhandling.py",
    "mock_db.py",
    "pool.py",
    "spec.py",
    "sql_render.py",
    "views.py",
    "sqlite_store.py",
}


def evaluate_read_file(
    args: dict, workspace_root: Path, range_hint: str = "StartLine and EndLine"
) -> tuple[str, str | None]:
    raw_path = args.get("Path") or args.get("path")
    if not raw_path:
        return "allow", None

    p = Path(raw_path)
    if not p.is_absolute():
        p = (workspace_root / p).resolve()
    else:
        p = p.resolve()

    filename_lower = p.name.lower()
    parts = [part.lower() for part in p.parts]

    raw_p = Path(raw_path)
    raw_filename_lower = raw_p.name.lower()
    raw_parts = [part.lower() for part in raw_p.parts]

    # 1. Forbidden files guardrail
    if filename_lower in FORBIDDEN_BASENAMES or raw_filename_lower in FORBIDDEN_BASENAMES:
        return (
            "deny",
            f"Blocked by project guardrails: '{raw_p.name}' is a forbidden file (token-save.md §2)."
        )

    # 2. Forbidden directory guardrails
    if "__pycache__" in parts or "__pycache__" in raw_parts:
        return (
            "deny",
            f"Blocked by project guardrails: '__pycache__' directory is forbidden (token-save.md §2)."
        )

    # Check if inside a directory named 'tool'
    if "tool" in parts or "tool" in raw_parts:
        return (
            "deny",
            f"Blocked by project guardrails: 'tool/' directory is forbidden (token-save.md §2)."
        )

    # 3. Upstream kernel repository documentation guardrail
    if ("repos" in parts or "repos" in raw_parts) and (filename_lower == "agents.md" or raw_filename_lower == "agents.md"):
        return (
            "deny",
            f"Blocked by project guardrails: '{raw_p.name}' inside repo directory is upstream documentation, not parser rules (token-save.md §2)."
        )

    # 4. Engine boundary guardrail & deprecated stub redirects
    if filename_lower == "api.md":
        if "table_engine" in parts:
            return (
                "deny",
                "Redirect: 'table_engine/API.md' is deprecated. Consult 'table_engine/TE_API.md' directly (token-save.md §1)."
            )
        if "db_engine" in parts:
            return (
                "deny",
                "Redirect: 'db_engine/API.md' is deprecated. Consult 'db_engine/DB_API.md' directly (token-save.md §1)."
            )

    if "db_engine" in parts:
        if filename_lower not in ALLOWED_DB_ENGINE_FILES:
            return (
                "deny",
                f"Blocked by engine boundary: '{p.name}' is inside 'db_engine/'. Only 'base.py' and 'DB_API.md' are allowed (token-save.md §2)."
            )

    # 5. Large file slice-read guardrail
    if p.is_file():
        try:
            file_size = p.stat().st_size
        except OSError:
            file_size = 0

        if file_size >= LARGE_FILE_THRESHOLD_BYTES:
            start_line = args.get("StartLine")
            end_line = args.get("EndLine")

            if start_line is None or end_line is None:
                size_kb = file_size // 1024
                rel_name = p.name
                return (
                    "deny",
                    f"Blocked by token-saving guardrail: '{rel_name}' is {size_kb} KB (>40 KB). "
                    f"Full-file reads are prohibited to protect the context budget. "
                    f"Please specify {range_hint} (span <= 300 lines), use grep -n, "
                    f"or run 'python3 .agents/scripts/spec_section.py <file> <section>'."
                )

            try:
                span = int(end_line) - int(start_line) + 1
            except (ValueError, TypeError):
                span = 0

            if span > MAX_SLICE_SPAN:
                return (
                    "deny",
                    f"Blocked by token-saving guardrail: Requested slice of '{p.name}' is {span} lines "
                    f"(maximum permitted for large files is {MAX_SLICE_SPAN} lines). "
                    f"Please request a narrower slice ({range_hint})."
                )

    return "allow", None


def claude_read_args(tool_input: dict) -> dict:
    """Translate Claude Code `Read` input (file_path, 1-based offset, limit) to guard args."""
    args = {"path": tool_input.get("file_path")}
    offset = tool_input.get("offset")
    limit = tool_input.get("limit")
    if offset is not None or limit is not None:
        try:
            start = int(offset) if offset is not None else 1
            # Claude's Read defaults to 2000 lines when limit is omitted.
            span = int(limit) if limit is not None else 2000
            args["StartLine"] = start
            args["EndLine"] = start + span - 1
        except (ValueError, TypeError):
            pass
    return args


def handle_claude(input_data: dict) -> None:
    """Claude Code PreToolUse: no output = allow; hookSpecificOutput deny blocks the call."""
    if input_data.get("tool_name") != "Read":
        return
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or input_data.get("cwd")
    workspace_root = Path(project_dir) if project_dir else Path.cwd()
    args = claude_read_args(input_data.get("tool_input") or {})
    decision, reason = evaluate_read_file(args, workspace_root, range_hint="Read offset and limit")
    if decision == "deny":
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }))


def handle_antigravity(input_data: dict) -> None:
    tool_call = input_data.get("toolCall", {})
    tool_name = tool_call.get("name", "")
    args = tool_call.get("args", {})

    workspaces = input_data.get("workspacePaths", [])
    workspace_root = Path(workspaces[0]) if workspaces else Path.cwd()

    if tool_name == "read_file":
        decision, reason = evaluate_read_file(args, workspace_root)
        if decision == "deny":
            res = {"decision": "deny", "reason": reason}
        else:
            res = {"decision": "allow"}
        print(json.dumps(res))
        return

    # All other tools allowed
    print(json.dumps({"decision": "allow"}))


def main():
    try:
        input_data = json.load(sys.stdin)
    except Exception:
        # Failsafe: if input parsing fails, allow so we never crash the agent
        print(json.dumps({"decision": "allow"}))
        return

    if "tool_name" in input_data and "toolCall" not in input_data:
        handle_claude(input_data)
    else:
        handle_antigravity(input_data)


if __name__ == "__main__":
    main()
