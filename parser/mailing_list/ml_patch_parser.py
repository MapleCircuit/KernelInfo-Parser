"""parser/mailing_list/ml_patch_parser.py - Git patch & unified diff email parser.

Parses RFC822 email text bodies into structured commit messages, diffstat,
and file hunks compatible with KernelInfo's diff rendering format.
"""
from __future__ import annotations

import re
from typing import Any

HUNK_HEADER_RE = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@(.*)$")
DIFF_GIT_RE = re.compile(r"^diff --git\s+a/(.+?)\s+b/(.+)$")
TRIPLE_MINUS_RE = re.compile(r"^---\s+(?:a/(.+)|\S+)")
TRIPLE_PLUS_RE = re.compile(r"^\+\+\+\s+(?:b/(.+)|\S+)")


def parse_patch_from_body(body_text: str) -> dict[str, Any]:
    """Parse email body into text commentary and structured file diffs.

    Returns:
        dict with keys:
            is_patch (bool): True if unified diff blocks were found.
            body (str): The introductory message/commentary before the diff.
            diff (dict | None): Structured diff matching WebApp's diff spec:
                files: list of file dicts (path, old_path, status, additions, deletions, hunks)
                additions: total lines added
                deletions: total lines deleted
    """
    if not body_text:
        return {"is_patch": False, "body": "", "diff": None}

    lines = body_text.splitlines()

    # Find the start of the first diff section (diff --git or --- a/ ... +++ b/)
    diff_start_idx = -1
    for i, line in enumerate(lines):
        if line.startswith("diff --git a/") or (
            line.startswith("--- a/")
            and i + 1 < len(lines)
            and lines[i + 1].startswith("+++ b/")
        ):
            diff_start_idx = i
            break

    if diff_start_idx == -1:
        return {"is_patch": False, "body": body_text, "diff": None}

    intro_lines = lines[:diff_start_idx]
    # Strip any trailing '---' separator line before diff
    while intro_lines and (intro_lines[-1].strip() == "---" or not intro_lines[-1].strip()):
        intro_lines.pop()
    intro_body = "\n".join(intro_lines)

    diff_lines = lines[diff_start_idx:]
    files: list[dict[str, Any]] = []
    current_file: dict[str, Any] | None = None
    current_hunk: dict[str, Any] | None = None

    old_no = 0
    new_no = 0
    total_adds = 0
    total_dels = 0

    idx = 0
    while idx < len(diff_lines):
        line = diff_lines[idx]

        # 1. New file diff header: diff --git a/path b/path
        m_git = DIFF_GIT_RE.match(line)
        if m_git:
            if current_file:
                if current_hunk:
                    current_file["hunks"].append(current_hunk)
                    current_hunk = None
                files.append(current_file)

            old_p = m_git.group(1).strip()
            new_p = m_git.group(2).strip()
            current_file = {
                "path": new_p,
                "old_path": old_p,
                "status": "M",
                "additions": 0,
                "deletions": 0,
                "binary": False,
                "hunks": [],
            }
            current_hunk = None
            idx += 1
            continue

        # Check for status indicators: new file mode, deleted file mode, rename
        if current_file:
            if line.startswith("new file mode"):
                current_file["status"] = "A"
            elif line.startswith("deleted file mode"):
                current_file["status"] = "D"
            elif line.startswith("similarity index"):
                current_file["status"] = "R"
            elif line.startswith("Binary files "):
                current_file["binary"] = True

        # Fallback file header without diff --git: --- a/path \n +++ b/path
        if line.startswith("--- a/") and idx + 1 < len(diff_lines) and diff_lines[idx + 1].startswith("+++ b/"):
            if not current_file or current_hunk is not None:
                if current_file:
                    if current_hunk:
                        current_file["hunks"].append(current_hunk)
                        current_hunk = None
                    files.append(current_file)
                old_p = line[6:].strip()
                new_p = diff_lines[idx + 1][6:].strip()
                current_file = {
                    "path": new_p,
                    "old_path": old_p,
                    "status": "M",
                    "additions": 0,
                    "deletions": 0,
                    "binary": False,
                    "hunks": [],
                }
                current_hunk = None
                idx += 2
                continue

        # 2. Hunk header: @@ -old_start,old_count +new_start,new_count @@ heading
        m_hunk = HUNK_HEADER_RE.match(line)
        if m_hunk:
            if current_file is None:
                # Synthetic file container if diff had no preceding diff --git
                current_file = {
                    "path": "patch",
                    "old_path": "patch",
                    "status": "M",
                    "additions": 0,
                    "deletions": 0,
                    "binary": False,
                    "hunks": [],
                }
            if current_hunk:
                current_file["hunks"].append(current_hunk)

            old_s = int(m_hunk.group(1))
            old_c = int(m_hunk.group(2)) if m_hunk.group(2) else 1
            new_s = int(m_hunk.group(3))
            new_c = int(m_hunk.group(4)) if m_hunk.group(4) else 1
            heading = m_hunk.group(5).strip()

            current_hunk = {
                "old_start": old_s,
                "old_count": old_c,
                "new_start": new_s,
                "new_count": new_c,
                "heading": heading,
                "lines": [],
            }
            old_no = old_s
            new_no = new_s
            idx += 1
            continue

        # 3. Diff content lines
        if current_hunk is not None and current_file is not None:
            # End of diff: signature delimiter '-- '
            if line == "-- " or line == "--":
                break

            if line.startswith("+"):
                current_file["additions"] += 1
                total_adds += 1
                current_hunk["lines"].append(["+", None, new_no, line[1:]])
                new_no += 1
            elif line.startswith("-"):
                current_file["deletions"] += 1
                total_dels += 1
                current_hunk["lines"].append(["-", old_no, None, line[1:]])
                old_no += 1
            elif line.startswith(" "):
                current_hunk["lines"].append([" ", old_no, new_no, line[1:]])
                old_no += 1
                new_no += 1
            elif line == "\\ No newline at end of file":
                pass
            else:
                # If non-diff line occurs after hunks, might be post-patch commentary or sign-off
                if line.startswith("diff --git"):
                    # Will be caught on next loop
                    continue
                # Context line without leading space
                current_hunk["lines"].append([" ", old_no, new_no, line])
                old_no += 1
                new_no += 1

        idx += 1

    if current_file:
        if current_hunk:
            current_file["hunks"].append(current_hunk)
        files.append(current_file)

    if not files:
        return {"is_patch": False, "body": body_text, "diff": None}

    return {
        "is_patch": True,
        "body": intro_body,
        "diff": {
            "files": files,
            "additions": total_adds,
            "deletions": total_dels,
        },
    }
