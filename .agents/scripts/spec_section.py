#!/usr/bin/env python3
"""spec_section.py - Fast targeted section lookup & TOC line-indexer for large specs.

Usage:
    # Print the text of section 4.8 from webapp/API_ENDPOINTS.md:
    python3 .agents/scripts/spec_section.py webapp/API_ENDPOINTS.md 4.8

    # Output only start and end line numbers (for use with read_file tool):
    python3 .agents/scripts/spec_section.py webapp/API_ENDPOINTS.md 4.8 --lines-only

    # Search section by name/heading:
    python3 .agents/scripts/spec_section.py core/API.md "Release Statistics"

    # Scan and update TOC line ranges [Lxxx-Lyyy] in lines 1-120 of the file:
    python3 .agents/scripts/spec_section.py webapp/API_ENDPOINTS.md --update-toc

    # Scan and update TOC line ranges across all subsystem spec files:
    python3 .agents/scripts/spec_section.py --update-all
"""

import sys
import re
from pathlib import Path


def parse_headings(lines: list[str]) -> list[dict]:
    headings = []
    in_code_block = False
    fence_pattern = re.compile(r"^(?:`{3,}|~{3,})")

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if fence_pattern.match(stripped):
            in_code_block = not in_code_block
            continue

        if in_code_block:
            continue

        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            headings.append({
                "line": idx,
                "level": level,
                "text": text,
                "raw": line,
            })
    return headings


def find_section(headings: list[dict], total_lines: int, query: str) -> tuple[int, int, str] | None:
    query_clean = query.strip().lstrip("§").strip()

    # Pattern for numeric section match (e.g., "4.8", "12", "5.21", "2B")
    # Matches "4.8", "4.8.", "§4.8", "§ 4.8", "4.8 ", "2B" at beginning of heading
    sec_num_pattern = re.compile(
        r"^(?:§\s*)?" + re.escape(query_clean) + r"(?:\.|\b|\s)",
        re.IGNORECASE
    )

    matched_idx = -1
    for i, h in enumerate(headings):
        if sec_num_pattern.search(h["text"]):
            matched_idx = i
            break

    # Fallback: substring in heading text (case-insensitive)
    if matched_idx == -1:
        query_lower = query_clean.lower()
        for i, h in enumerate(headings):
            if query_lower in h["text"].lower():
                matched_idx = i
                break

    if matched_idx == -1:
        return None

    target = headings[matched_idx]
    start_line = target["line"]
    target_level = target["level"]

    # End line is right before next heading of same or higher level (fewer or equal '#'s)
    end_line = total_lines
    for next_h in headings[matched_idx + 1:]:
        if next_h["level"] <= target_level:
            end_line = next_h["line"] - 1
            break

    return start_line, end_line, target["text"]


def update_toc(filepath: Path) -> bool:
    content = filepath.read_text(encoding="utf-8")
    lines = content.splitlines(keepends=True)
    headings = parse_headings([l.rstrip("\r\n") for l in lines])

    changed = False
    new_lines = []

    # Process TOC in first 120 lines
    for idx, raw_line in enumerate(lines, 1):
        if idx > 120:
            new_lines.append(raw_line)
            continue

        # Check if line contains any [L...]
        if not re.search(r"\[L\d+(?:-L\d+)?\]", raw_line):
            new_lines.append(raw_line)
            continue

        # Split line by '|' to support multiple entries on one line
        segments = raw_line.split("|")
        new_segments = []
        line_changed = False

        for seg in segments:
            range_match = re.search(r"\[L(\d+)(?:-L(\d+))?\]", seg)
            if range_match:
                before = seg[:range_match.start()]
                sec_m = re.search(r"(?:§\s*|`|\*\*)(\d+[A-Za-z]?(?:\.\d+)*)", before)
                if not sec_m:
                    sec_m = re.search(r"^\s*-\s*(\d+[A-Za-z]?(?:\.\d+)*)", before)

                if sec_m:
                    sec_key = sec_m.group(1)
                    sec_res = find_section(headings, len(lines), sec_key)
                    if sec_res:
                        s, e, _ = sec_res
                        if range_match.group(2):
                            new_tag = f"[L{s}-L{e}]"
                        else:
                            new_tag = f"[L{s}]"

                        if new_tag != range_match.group(0):
                            seg = seg[:range_match.start()] + new_tag + seg[range_match.end():]
                            line_changed = True

            new_segments.append(seg)

        if line_changed:
            updated_line = "|".join(new_segments)
            changed = True
        else:
            updated_line = raw_line

        new_lines.append(updated_line)

    if changed:
        filepath.write_text("".join(new_lines), encoding="utf-8")
        print(f"Updated TOC line indices in {filepath}")
    else:
        print(f"TOC line indices in {filepath} are already up to date.")
    return changed


SPEC_FILES = [
    "core/API.md",
    "webapp/API.md",
    "webapp/API_ENDPOINTS.md",
    "webapp/API_FRONTEND.md",
    "parser/c_ast/CAST_API.md",
    "parser/kconfig_ast/KCONFIG_API.md",
    "parser/rust_ast/RUST_API.md",
    "parser/git_ast/GIT_API.md",
    "parser/maintainer_ast/MAINTAINER_API.md",
    "table_engine/TE_API.md",
    "db_engine/DB_API.md",
]


def update_all() -> None:
    for spec_rel in SPEC_FILES:
        p = Path(spec_rel)
        if p.is_file():
            update_toc(p)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    if "--update-all" in sys.argv:
        update_all()
        sys.exit(0)

    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"Error: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    if "--update-toc" in sys.argv:
        update_toc(path)
        sys.exit(0)

    if len(sys.argv) < 3:
        print("Error: query required (e.g. section number '4.8' or title)", file=sys.stderr)
        sys.exit(1)

    query = sys.argv[2]
    lines_only = "--lines-only" in sys.argv

    text = path.read_text(encoding="utf-8")
    raw_lines = text.splitlines()
    headings = parse_headings(raw_lines)

    res = find_section(headings, len(raw_lines), query)
    if not res:
        print(f"Error: section '{query}' not found in {path}", file=sys.stderr)
        sys.exit(1)

    start_line, end_line, heading_title = res

    if lines_only:
        print(f"{start_line} {end_line}")
    else:
        print(f"# {path} (Lines {start_line}-{end_line}): {heading_title}")
        print("=" * 80)
        for idx in range(start_line - 1, end_line):
            print(f"{idx + 1:4d}: {raw_lines[idx]}")


if __name__ == "__main__":
    main()
